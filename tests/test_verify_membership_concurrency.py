"""Portable real-transaction races; PostgreSQL uses its actual token row lock."""
from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from sqlalchemy import select

from anticheatbot.db.models import UserChatState, VerifyToken
from anticheatbot.db.session import configure_engine, session_scope
from anticheatbot.handlers import verify_flow
from anticheatbot.services.groups import set_member_verification_exempt
from anticheatbot.time_utils import utc_now
from anticheatbot.web import verify_api
from tests.test_verify_api_transactions import CHAT, TOKEN, USER, _request, _seed


async def run_enabled_membership_race(
    database_url: str, outcome: str = "quiz_fail", change: str = "join", stage: str = "member",
) -> None:
    """Use a fresh disposable DB per call. No real Telegram calls or credentials.

    outcome: rules_ok/quiz_ok/quiz_fail; change: join/leave/promotion;
    stage: member/turnstile. The caller creates and deletes the isolated DB.
    """
    assert outcome in ("rules_ok", "quiz_ok", "quiz_fail")
    assert change in ("join", "leave", "promotion")
    assert stage in ("member", "turnstile")
    engine = configure_engine(database_url)
    patch = pytest.MonkeyPatch()
    api_task = changed_task = None
    entered, release = asyncio.Event(), asyncio.Event()
    stack = AsyncExitStack()
    try:
        await _seed("rules_ack" if outcome == "rules_ok" else "quiz")
        date = utc_now().replace(microsecond=0)
        async with session_scope() as session:
            state = await session.get(UserChatState, (CHAT, USER))
            state.membership_status = "joined"
            state.membership_update_id = 100
            state.membership_event_at = date-timedelta(seconds=10)
            state.membership_join_sources = 1
            state.llm_messages_seen = 0
            (await session.get(VerifyToken, TOKEN)).created_at = date-timedelta(seconds=5)
        bot = AsyncMock()
        request = _request(bot, answers=None if outcome == "rules_ok" else {"1": int(outcome == "quiz_fail")})
        patch.setattr(verify_flow, "get_settings", lambda: request.app["settings"])
        patch.setattr(verify_flow, "schedule_verification_timeout", Mock())
        patch.setattr(verify_flow, "_send_verify_invite_message", AsyncMock(
            return_value=SimpleNamespace(chat=SimpleNamespace(id=CHAT), message_id=77),
        ))
        set_member_verification_exempt(CHAT, USER, False)
        first_member = True

        async def member(*args, **kwargs):
            nonlocal first_member
            if stage == "member" and first_member:
                first_member = False
                entered.set()
                await release.wait()
            return SimpleNamespace(status="restricted", is_member=True)

        async def turnstile(*args, **kwargs):
            entered.set()
            await release.wait()

        bot.get_chat_member.side_effect = member
        if stage == "turnstile":
            patch.setattr(verify_api, "_turnstile_or_403", turnstile)
        # SQLite has no SELECT FOR UPDATE. Only the local smoke test needs an
        # artificial member gate; PostgreSQL exercises the real token lock order.
        if database_url.startswith("sqlite"):
            await stack.enter_async_context(verify_flow._member_lock(CHAT, USER))
        handler = verify_api.verify_rules_complete if outcome == "rules_ok" else verify_api.verify_quiz_submit
        api_task = asyncio.create_task(handler(request))
        await asyncio.wait_for(entered.wait(), timeout=3)
        user = SimpleNamespace(id=USER, is_bot=False, language_code="en")
        event = SimpleNamespace(
            chat=SimpleNamespace(id=CHAT, type="supergroup", title=None), date=date,
            from_user=user, via_join_request=False, invite_link=None,
            old_chat_member=SimpleNamespace(status="left" if change == "join" else "member", user=user),
            new_chat_member=SimpleNamespace(status={"join": "member", "leave": "left", "promotion": "administrator"}[change], user=user),
        )
        callback = {"join": verify_flow.on_user_joined, "leave": verify_flow.on_member_departed, "promotion": verify_flow.on_member_promoted}[change]
        changed_task = asyncio.create_task(callback(event, bot, event_update=SimpleNamespace(update_id=101)))

        async def early_context():
            while True:
                notices = verify_flow._pending_join_events.get((CHAT, USER), []) if change == "join" else [
                    verify_flow._pending_membership_retirements.get((CHAT, USER))
                ]
                if any(notice is not None and notice.update_id == 101 for notice in notices):
                    return
                if changed_task.done():
                    await changed_task
                    raise AssertionError("membership event finished while API owns token")
                await asyncio.sleep(0)

        await asyncio.wait_for(early_context(), timeout=3)
        assert not api_task.done() and not changed_task.done()
        release.set()
        with pytest.raises(web.HTTPForbidden) as error:
            await asyncio.wait_for(api_task, timeout=3)
        assert json.loads(error.value.text) == {"error": "membership_changed"}
        bot.ban_chat_member.assert_not_awaited()
        bot.unban_chat_member.assert_not_awaited()
        assert all(call.kwargs["permissions"].can_send_messages is False for call in bot.restrict_chat_member.await_args_list)
        await stack.aclose()
        await asyncio.wait_for(changed_task, timeout=3)
        async with session_scope() as session:
            old = await session.get(VerifyToken, TOKEN)
            state = await session.get(UserChatState, (CHAT, USER))
            assert old.used_at is not None
            assert state.verified is (change == "promotion")
            assert (state.verified_at is not None) is (change == "promotion")
            assert state.llm_messages_seen == 0
            if change == "join":
                live = (await session.scalars(select(VerifyToken).where(VerifyToken.used_at.is_(None)))).one()
                assert live.token != TOKEN and live.created_at > old.created_at
        if change == "join":
            bot.restrict_chat_member.assert_awaited_once()
            assert bot.restrict_chat_member.call_args.kwargs["permissions"].can_send_messages is False
        else:
            bot.restrict_chat_member.assert_not_awaited()
    finally:
        release.set()
        await stack.aclose()
        for task in (api_task, changed_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        set_member_verification_exempt(CHAT, USER, False)
        patch.undo()
        await engine.dispose()


@pytest.mark.parametrize("outcome", ["rules_ok", "quiz_ok", "quiz_fail"])
@pytest.mark.parametrize("stage", ["member", "turnstile"])
def test_old_completion_cannot_cross_membership_boundary(tmp_path, outcome, stage):
    asyncio.run(run_enabled_membership_race(f"sqlite+aiosqlite:///{tmp_path}/race.sqlite", outcome, "join", stage))
