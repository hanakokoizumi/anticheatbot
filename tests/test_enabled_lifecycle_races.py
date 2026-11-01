"""Real dispatcher events must invalidate an older HTTP permission decision."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace
from weakref import WeakValueDictionary

import pytest
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from anticheatbot.db.models import GroupSettings, UserChatState, VerifyToken
from anticheatbot.db.session import configure_engine, session_scope
from anticheatbot.handlers import guest_bots, moderation, setup_handlers, verify_flow
from anticheatbot.services.groups import set_member_verification_exempt
from anticheatbot.time_utils import utc_now
from anticheatbot.web import verify_api
from tests.test_verify_api_transactions import CHAT, TOKEN, USER, _request, _seed


def _membership_update(change, date):
    user = {"id": USER, "is_bot": False, "first_name": "Audit"}
    old = {"status": "left" if change == "join" else "member", "user": user}
    new = {"status": {"join": "member", "leave": "left", "promotion": "administrator"}[change], "user": user}
    if change == "promotion":
        for permission in (
            "can_be_edited", "is_anonymous", "can_manage_chat", "can_delete_messages",
            "can_manage_video_chats", "can_restrict_members", "can_promote_members",
            "can_change_info", "can_invite_users", "can_post_stories", "can_edit_stories",
            "can_delete_stories",
        ):
            new[permission] = False
    return Update.model_validate({
        "update_id": 101,
        "chat_member": {
            "chat": {"id": CHAT, "type": "supergroup", "title": "Audit"},
            "from": user, "date": date, "old_chat_member": old, "new_chat_member": new,
        },
    })


@asynccontextmanager
async def _case(monkeypatch, tmp_path, outcome):
    # Each synchronous pytest case owns a separate asyncio.run loop. Cancelled-task
    # tracebacks can retain weak-map entries until GC, so isolate in-flight state.
    for name in ("_member_locks", "_membership_versions"):
        monkeypatch.setattr(verify_flow, name, WeakValueDictionary())
    for name in ("_pending_join_events", "_pending_membership_retirements"):
        monkeypatch.setattr(verify_flow, name, {})
    for name in ("_moderation_locks", "_message_versions"):
        monkeypatch.setattr(moderation, name, WeakValueDictionary())
    engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path / 'enabled-lifecycle.sqlite'}")
    bot, dp = Bot("12345:fake"), Dispatcher()
    try:
        await _seed("rules_ack" if outcome == "rules_ok" else "quiz")
        date = utc_now().replace(microsecond=0)
        async with session_scope() as session:
            state = await session.get(UserChatState, (CHAT, USER))
            state.membership_status = "joined"
            state.membership_update_id = 100
            state.membership_event_at = date - timedelta(seconds=10)
            state.membership_join_sources = 1
            (await session.get(VerifyToken, TOKEN)).created_at = date - timedelta(seconds=5)
        answers = None if outcome == "rules_ok" else {"1": int(outcome == "quiz_fail")}
        request = _request(bot, answers=answers)
        monkeypatch.setattr(verify_flow, "get_settings", lambda: request.app["settings"])
        monkeypatch.setattr(guest_bots, "get_settings", lambda: request.app["settings"])
        monkeypatch.setattr(verify_flow, "schedule_verification_timeout", Mock())
        monkeypatch.setattr(verify_flow, "_send_verify_invite_message", AsyncMock(
            return_value=SimpleNamespace(chat=SimpleNamespace(id=CHAT), message_id=77),
        ))
        for method in ("restrict_chat_member", "ban_chat_member", "unban_chat_member", "delete_message"):
            monkeypatch.setattr(bot, method, AsyncMock(return_value=True))
        monkeypatch.setattr(bot, "get_chat_member", AsyncMock(
            return_value=SimpleNamespace(status="restricted", is_member=True),
        ))
        set_member_verification_exempt(CHAT, USER, False)
        setup_handlers(dp)
        handler = verify_api.verify_rules_complete if outcome == "rules_ok" else verify_api.verify_quiz_submit
        yield SimpleNamespace(bot=bot, dp=dp, request=request, handler=handler, date=date)
    finally:
        for router in dp.sub_routers:
            router._parent_router = None
        dp.sub_routers.clear()
        set_member_verification_exempt(CHAT, USER, False)
        await dp.storage.close()
        await bot.session.close()
        await engine.dispose()


async def _wait_for_early_context(change):
    """The handler must really run, while its persistence waits for the member lock."""
    async def wait():
        while True:
            if change == "join":
                notices = verify_flow._pending_join_events.get((CHAT, USER), [])
                if any(notice.update_id == 101 for notice in notices):
                    return
            else:
                notice = verify_flow._pending_membership_retirements.get((CHAT, USER))
                if notice is not None and notice.update_id == 101:
                    return
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), timeout=2)


@pytest.mark.parametrize("outcome", ["rules_ok", "quiz_ok", "quiz_fail"])
@pytest.mark.parametrize("change", ["join", "leave", "promotion"])
def test_enabled_api_rechecks_real_membership_events_after_slow_rpc(monkeypatch, tmp_path, outcome, change):
    async def run():
        changed_task = None
        async with _case(monkeypatch, tmp_path, outcome) as ctx:
            try:
                async def stale_membership(*args, **kwargs):
                    nonlocal changed_task
                    if changed_task is None:
                        changed_task = asyncio.create_task(ctx.dp.feed_update(
                            ctx.bot, _membership_update(change, ctx.date),
                        ))
                        await _wait_for_early_context(change)
                        assert not changed_task.done()
                    # This response describes the old membership. The newer update has
                    # already entered the actual dispatcher but cannot yet write state.
                    return SimpleNamespace(status="restricted", is_member=True)

                ctx.bot.get_chat_member.side_effect = stale_membership
                async with verify_flow._member_lock(CHAT, USER):
                    with pytest.raises(web.HTTPForbidden) as caught:
                        await asyncio.wait_for(ctx.handler(ctx.request), timeout=3)
                    assert json.loads(caught.value.text)["error"] == "membership_changed"
                    ctx.bot.restrict_chat_member.assert_not_awaited()
                    ctx.bot.ban_chat_member.assert_not_awaited()
                    ctx.bot.unban_chat_member.assert_not_awaited()
                    # Rejected completion must roll back its own success/consumption.
                    async with session_scope() as session:
                        assert (await session.get(VerifyToken, TOKEN)).used_at is None
                        assert not (await session.get(UserChatState, (CHAT, USER))).verified

                await asyncio.wait_for(changed_task, timeout=3)
                async with session_scope() as session:
                    assert (await session.get(VerifyToken, TOKEN)).used_at is not None
                    state = await session.get(UserChatState, (CHAT, USER))
                    assert state.verified is (change == "promotion")
                    assert state.membership_status == {"join": "joined", "leave": "left", "promotion": "admin"}[change]
                    live = (await session.scalars(select(VerifyToken).where(VerifyToken.used_at.is_(None)))).all()
                    assert len(live) == int(change == "join")
                    if live:
                        assert live[0].token != TOKEN
                assert ctx.bot.restrict_chat_member.await_count == int(change == "join")
                if change == "join":
                    assert ctx.bot.restrict_chat_member.await_args.kwargs["permissions"].can_send_messages is False
                ctx.bot.ban_chat_member.assert_not_awaited()
                ctx.bot.unban_chat_member.assert_not_awaited()
            finally:
                if changed_task is not None and not changed_task.done():
                    changed_task.cancel()
                    await asyncio.gather(changed_task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["rules_ok", "quiz_ok", "quiz_fail"])
def test_current_transaction_consumed_or_verified_state_still_allows_action(monkeypatch, tmp_path, outcome):
    async def run():
        async with _case(monkeypatch, tmp_path, outcome) as ctx:
            response = await ctx.handler(ctx.request)
            failed = outcome == "quiz_fail"
            assert response.status == (403 if failed else 200)
            assert json.loads(response.text)["ok"] is not failed
            assert ctx.bot.restrict_chat_member.await_count == int(not failed)
            if not failed:
                assert ctx.bot.restrict_chat_member.await_args.kwargs["permissions"].can_send_messages is True
            assert ctx.bot.ban_chat_member.await_count == int(failed)
            assert ctx.bot.unban_chat_member.await_count == int(failed)
            async with session_scope() as session:
                assert (await session.get(VerifyToken, TOKEN)).used_at is not None
                state = await session.get(UserChatState, (CHAT, USER))
                assert state.verified is not failed
                assert (state.verified_at is not None) is not failed
                assert state.llm_messages_seen == 0
    asyncio.run(run())


@pytest.mark.parametrize("change", ["join", "leave", "promotion"])
def test_failed_invite_release_rechecks_real_event_after_member_rpc(monkeypatch, tmp_path, change):
    async def run():
        changed_task = None
        async with _case(monkeypatch, tmp_path, "rules_ok") as ctx:
            try:
                async def stale_membership(*args, **kwargs):
                    nonlocal changed_task
                    changed_task = asyncio.create_task(ctx.dp.feed_update(
                        ctx.bot, _membership_update(change, ctx.date),
                    ))
                    await _wait_for_early_context(change)
                    return SimpleNamespace(status="restricted", is_member=True)

                ctx.bot.get_chat_member.side_effect = stale_membership
                async with verify_flow._member_lock(CHAT, USER):
                    assert not await verify_flow._release_after_invite_failure(ctx.bot, TOKEN)
                    ctx.bot.restrict_chat_member.assert_not_awaited()
                    async with session_scope() as session:
                        assert (await session.get(VerifyToken, TOKEN)).used_at is None
                        assert not (await session.get(UserChatState, (CHAT, USER))).verified
                    changed_task.cancel()
                    await asyncio.gather(changed_task, return_exceptions=True)
            finally:
                if changed_task is not None and not changed_task.done():
                    changed_task.cancel()
                    await asyncio.gather(changed_task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["model", "member_rpc", "delete_rpc"])
@pytest.mark.parametrize("change", ["join", "leave", "promotion"])
def test_old_moderation_does_not_restrict_a_new_lifecycle(monkeypatch, tmp_path, stage, change):
    async def run():
        changed_task = None
        async with _case(monkeypatch, tmp_path, "rules_ok") as ctx:
            try:
                async with session_scope() as session:
                    state = await session.get(UserChatState, (CHAT, USER))
                    state.verified = True
                    state.verified_at = ctx.date - timedelta(seconds=1)
                    (await session.get(VerifyToken, TOKEN)).used_at = state.verified_at
                    group = await session.get(GroupSettings, CHAT)
                    group.llm_enabled = True
                    group.llm_max_messages = 5
                    group.spam_action = "delete_and_restrict"
                ctx.request.app["settings"].openai_api_key = "test"
                monkeypatch.setattr(moderation, "get_settings", lambda: ctx.request.app["settings"])

                async def start_new_event():
                    nonlocal changed_task
                    if changed_task is None:
                        changed_task = asyncio.create_task(ctx.dp.feed_update(
                            ctx.bot, _membership_update(change, ctx.date),
                        ))
                        await _wait_for_early_context(change)
                        assert not changed_task.done()

                async def model(*args, **kwargs):
                    if stage == "model":
                        await start_new_event()
                    return {"label": "spam", "confidence": 1}

                member_calls = 0

                async def member(*args, **kwargs):
                    nonlocal member_calls
                    member_calls += 1
                    if stage == "member_rpc" and member_calls == 2:
                        await start_new_event()
                    return SimpleNamespace(status="member", is_member=True)

                async def delete(*args, **kwargs):
                    if stage == "delete_rpc":
                        await start_new_event()
                    return True

                monkeypatch.setattr(moderation, "moderate_message", AsyncMock(side_effect=model))
                ctx.bot.get_chat_member.side_effect = member
                ctx.bot.delete_message.side_effect = delete
                message = Update.model_validate({
                    "update_id": 99,
                    "message": {
                        "message_id": 42, "date": ctx.date - timedelta(seconds=1),
                        "chat": {"id": CHAT, "type": "supergroup", "title": "Audit"},
                        "from": {"id": USER, "is_bot": False, "first_name": "Audit"},
                        "text": "A spam message from the prior membership",
                    },
                })
                async with verify_flow._member_lock(CHAT, USER):
                    await asyncio.wait_for(ctx.dp.feed_update(ctx.bot, message), timeout=3)
                    assert changed_task is not None, "test did not reach the intended RPC boundary"
                    ctx.bot.restrict_chat_member.assert_not_awaited()
                    ctx.bot.ban_chat_member.assert_not_awaited()
                    ctx.bot.unban_chat_member.assert_not_awaited()
                    if stage != "delete_rpc":
                        ctx.bot.delete_message.assert_not_awaited()
                    changed_task.cancel()
                    await asyncio.gather(changed_task, return_exceptions=True)
            finally:
                if changed_task is not None and not changed_task.done():
                    changed_task.cancel()
                    await asyncio.gather(changed_task, return_exceptions=True)
    asyncio.run(run())
