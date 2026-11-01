from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import RestrictChatMember

from anticheatbot.db.models import GroupSettings, KnownChat, UserChatState, VerifyToken
from anticheatbot.db.session import configure_engine, session_scope
from anticheatbot.handlers import verify_flow
from anticheatbot.telegram_perms import normal_member_permissions
from anticheatbot.time_utils import utc_now
from anticheatbot.web import admin_api, verify_api
from tests.signing import sign_webapp_init_data
from tests.test_verify_api_transactions import CHAT, TOKEN, USER, _request, _seed


def _bot():
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(status="restricted", is_member=True)
    return bot


async def _disable():
    async with session_scope() as session:
        (await session.get(GroupSettings, CHAT)).verification_enabled = False


def _patch_request(bot):
    request = _request(bot)
    request.app["settings"].global_admin_user_ids = str(USER)
    request.rel_url = SimpleNamespace(query={"chat_id": str(CHAT)})
    request.json = AsyncMock(return_value={"verification_enabled": False})
    return request


@pytest.mark.parametrize("entry", ["session", "rules", "quiz"])
def test_disabled_endpoints_restore_without_challenge_or_answers(tmp_path, monkeypatch, entry):
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/disabled.sqlite")
        try:
            await _seed("quiz" if entry == "quiz" else "rules_ack")
            await _disable()
            # Expired current attempts also need release; there is no quiz requirement now.
            async with session_scope() as session:
                (await session.get(VerifyToken, TOKEN)).expires_at = utc_now()-timedelta(seconds=1)
                (await session.get(GroupSettings, CHAT)).turnstile_enabled = True
            challenge = AsyncMock(side_effect=AssertionError("disabled verification must not run Turnstile"))
            monkeypatch.setattr(verify_api, "_turnstile_or_403", challenge)
            bot = _bot()
            request = _request(bot)
            request.rel_url = SimpleNamespace(query={"t": TOKEN})
            handler = {"session": verify_api.verify_session, "rules": verify_api.verify_rules_complete, "quiz": verify_api.verify_quiz_submit}[entry]
            response = await handler(request)
            assert response.status == 200
            assert json.loads(response.text) == {"ok": True, "verification_disabled": True}
            bot.restrict_chat_member.assert_awaited_once()
            assert bot.restrict_chat_member.call_args.kwargs["permissions"].can_send_messages is True
            bot.ban_chat_member.assert_not_awaited()
            challenge.assert_not_awaited()
            async with session_scope() as session:
                assert (await session.get(VerifyToken, TOKEN)).used_at is not None
                assert (await session.get(UserChatState, (CHAT, USER))).verified is True
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_disabled_wrong_subject_still_cannot_use_token(tmp_path):
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/disabled.sqlite")
        try:
            await _seed()
            await _disable()
            bot = _bot()
            request = _request(bot)
            request.headers["X-Telegram-Init-Data"] = sign_webapp_init_data(bot_token=request.app["settings"].bot_token, user={"id": USER+1})
            with pytest.raises(web.HTTPForbidden):
                await verify_api.verify_rules_complete(request)
            bot.get_chat_member.assert_not_awaited()
            async with session_scope() as session:
                assert (await session.get(VerifyToken, TOKEN)).used_at is None
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["left", "kicked", "administrator", "new_token", "new_join"])
def test_disabled_recovery_does_not_restore_other_membership(tmp_path, case):
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/disabled.sqlite")
        pending = None
        try:
            await _seed()
            await _disable()
            bot = _bot()
            if case in ("left", "kicked", "administrator"):
                bot.get_chat_member.return_value.status = case
            elif case == "new_token":
                async with session_scope() as session:
                    session.add(VerifyToken(token="new-current-token-12345678", chat_id=CHAT, user_id=USER,
                        created_at=utc_now()+timedelta(seconds=1), expires_at=utc_now()+timedelta(minutes=10)))
            else:
                pending = verify_flow._begin_pending_join(CHAT, USER, source=1, event_at=utc_now()+timedelta(seconds=1), update_id=999)
            response = await verify_api.verify_rules_complete(_request(bot))
            assert json.loads(response.text)["verification_disabled"] is True
            bot.restrict_chat_member.assert_not_awaited()
            bot.unban_chat_member.assert_not_awaited()
            async with session_scope() as session:
                if case != "administrator":
                    assert (await session.get(UserChatState, (CHAT, USER))).verified is False
                if case == "new_token":
                    assert (await session.get(VerifyToken, "new-current-token-12345678")).used_at is None
        finally:
            if pending is not None:
                verify_flow._end_pending_join(CHAT, USER, pending)
            await engine.dispose()
    asyncio.run(run())


def test_disabled_release_failure_remains_retryable(tmp_path):
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/disabled.sqlite")
        try:
            await _seed()
            await _disable()
            bot = _bot()
            bot.restrict_chat_member.side_effect = TelegramNetworkError(
                method=RestrictChatMember(chat_id=CHAT, user_id=USER, permissions=normal_member_permissions()), message="offline",
            )
            with pytest.raises(web.HTTPServiceUnavailable):
                await verify_api.verify_rules_complete(_request(bot))
            async with session_scope() as session:
                assert (await session.get(VerifyToken, TOKEN)).used_at is None
                assert (await session.get(UserChatState, (CHAT, USER))).verified is False
            bot.restrict_chat_member.side_effect = None
            assert json.loads((await verify_api.verify_rules_complete(_request(bot))).text)["verification_disabled"]
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["disabled_before", "disabled_during_member", "reenabled_during_release"])
def test_timeout_uses_current_switch_and_recovers_before_old_deadline(tmp_path, monkeypatch, phase):
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/disabled.sqlite")
        try:
            await _seed()
            if phase != "disabled_during_member":
                await _disable()
            else:
                async with session_scope() as session:
                    (await session.get(VerifyToken, TOKEN)).expires_at = utc_now()-timedelta(seconds=1)
            bot = _bot()
            if phase != "disabled_before":
                async def change_policy(*args):
                    async with session_scope() as session:
                        (await session.get(GroupSettings, CHAT)).verification_enabled = phase == "reenabled_during_release"
                    return SimpleNamespace(status="restricted", is_member=True)
                bot.get_chat_member.side_effect = change_policy
            schedule = Mock()
            monkeypatch.setattr(verify_flow, "schedule_verification_timeout", schedule)
            await verify_flow._expire_verification(bot, chat_id=CHAT, user_id=USER, token=TOKEN, kick=True)
            bot.ban_chat_member.assert_not_awaited()
            async with session_scope() as session:
                token = await session.get(VerifyToken, TOKEN)
                if phase == "reenabled_during_release":
                    assert token.used_at is None
                    bot.restrict_chat_member.assert_not_awaited()
                    assert schedule.call_args.kwargs["delay"] > 0
                else:
                    assert token.used_at is not None
                    bot.restrict_chat_member.assert_awaited_once()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_admin_disable_only_schedules_target_group_and_does_not_wait_for_telegram(tmp_path, monkeypatch):
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/disabled.sqlite")
        try:
            await _seed()
            async with session_scope() as session:
                session.add(KnownChat(chat_id=CHAT-1))
                await session.flush()
                session.add(GroupSettings(chat_id=CHAT-1))
                session.add(VerifyToken(token="other-group-token-12345678", chat_id=CHAT-1, user_id=USER, expires_at=utc_now()+timedelta(minutes=5)))
            schedule = Mock()
            monkeypatch.setattr(verify_flow, "schedule_verification_timeout", schedule)
            bot = _bot()
            response = await admin_api.admin_settings_patch(_patch_request(bot))
            assert response.status == 200
            schedule.assert_called_once()
            assert schedule.call_args.kwargs["token"] == TOKEN
            assert schedule.call_args.kwargs["delay"] == 0
            bot.get_chat_member.assert_not_awaited()
            bot.restrict_chat_member.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


async def run_disable_race(database_url: str, stage: str) -> None:
    """Run against a fresh isolated PostgreSQL DB; stage is turnstile/member/reopen.

    No real Telegram calls. The caller supplies a disposable DB and must clean it up.
    """
    engine = configure_engine(database_url)
    patch = pytest.MonkeyPatch()
    task = None
    release = asyncio.Event()
    try:
        await _seed("quiz")
        bot = _bot()
        entered = asyncio.Event()
        if stage == "turnstile":
            async def challenge(*args):
                entered.set()
                await release.wait()
            patch.setattr(verify_api, "_turnstile_or_403", challenge)
        else:
            count = 0
            async def member(*args):
                nonlocal count
                count += 1
                if count == 1:
                    entered.set()
                    await release.wait()
                elif stage == "reopen":
                    async with session_scope() as session:
                        (await session.get(GroupSettings, CHAT)).verification_enabled = True
                return SimpleNamespace(status="restricted", is_member=True)
            bot.get_chat_member.side_effect = member
        patch.setattr(verify_flow, "schedule_verification_timeout", Mock())
        task = asyncio.create_task(verify_api.verify_quiz_submit(_request(bot, answers={"1": 1})))
        await asyncio.wait_for(entered.wait(), timeout=3)
        assert not task.done()
        # The POST still owns its token lock. Saving the group setting must complete
        # before the external request is released, without waiting for that lock.
        assert (await asyncio.wait_for(admin_api.admin_settings_patch(_patch_request(bot)), timeout=3)).status == 200
        assert not task.done()
        release.set()
        if stage == "reopen":
            with pytest.raises(web.HTTPServiceUnavailable):
                await asyncio.wait_for(task, timeout=3)
        else:
            response = await asyncio.wait_for(task, timeout=3)
            assert json.loads(response.text) == {"ok": True, "verification_disabled": True}
        bot.ban_chat_member.assert_not_awaited()
        async with session_scope() as session:
            token = await session.get(VerifyToken, TOKEN)
            state = await session.get(UserChatState, (CHAT, USER))
            assert (token.used_at is None) == (stage == "reopen")
            assert state.verified is (stage != "reopen")
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        patch.undo()
        await engine.dispose()


def test_slow_challenge_disable_race_with_separate_connections(tmp_path):
    asyncio.run(run_disable_race(f"sqlite+aiosqlite:///{tmp_path}/race.sqlite", "turnstile"))
