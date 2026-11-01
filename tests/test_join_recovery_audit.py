"""Independent checks of membership races using separate database connections."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import RestrictChatMember
from sqlalchemy import select

from anticheatbot import telegram_retry
from anticheatbot.db.models import GroupSettings, KnownChat, UserChatState, VerifyToken
from anticheatbot.db.session import configure_engine, create_all, session_scope
from anticheatbot.handlers import guest_bots, verify_flow
from anticheatbot.services.groups import member_mute_pending, set_member_verification_exempt
from anticheatbot.telegram_perms import muted_member_permissions
from anticheatbot.time_utils import utc_now

CHAT = -10099887766
USER = 10001


async def _prepare(monkeypatch, tmp_path):
    engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path / 'join-recovery.sqlite'}")
    await create_all()
    async with session_scope() as session:
        session.add(KnownChat(chat_id=CHAT, title="Audit"))
        session.add(GroupSettings(chat_id=CHAT, verification_enabled=True, verify_timeout_seconds=120))
    for uid in (USER, USER+1):
        set_member_verification_exempt(CHAT, uid, False)
    settings = SimpleNamespace(global_admin_id_set=set())
    monkeypatch.setattr(verify_flow, "get_settings", lambda: settings)
    monkeypatch.setattr(guest_bots, "get_settings", lambda: settings)
    invite = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=CHAT), message_id=77))
    monkeypatch.setattr(verify_flow, "_send_verify_invite_message", invite)
    monkeypatch.setattr(verify_flow, "schedule_verification_timeout", Mock())
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member", is_member=True)),
        restrict_chat_member=AsyncMock(), delete_message=AsyncMock(),
    )
    return engine, bot, invite


def test_mute_request_has_durable_recovery_token_and_unverified_state(monkeypatch, tmp_path):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        try:
            async def observe_restriction(*args, **kwargs):
                assert member_mute_pending(CHAT, USER)
                # A fresh connection cannot see an uncommitted provisional token.
                async with session_scope() as independent_session:
                    tokens = (await independent_session.scalars(select(VerifyToken))).all()
                    assert len(tokens) == 1
                    assert tokens[0].used_at is None
                    state = await independent_session.get(UserChatState, (CHAT, USER))
                    assert state is not None and state.verified is False
                assert kwargs["permissions"].can_send_messages is False

            bot.restrict_chat_member.side_effect = observe_restriction
            await verify_flow._start_verification_for_member(bot, chat_id=CHAT, user_id=USER, chat_title="Audit")
            bot.restrict_chat_member.assert_awaited_once()
            invite.assert_awaited_once()
            assert not member_mute_pending(CHAT, USER)
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["promoted", "left", "expired", "verified"])
def test_rate_limit_retry_rechecks_current_token_and_membership(monkeypatch, tmp_path, change):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        try:
            method = RestrictChatMember(chat_id=CHAT, user_id=USER, permissions=muted_member_permissions())
            bot.restrict_chat_member.side_effect = TelegramRetryAfter(method=method, message="flood", retry_after=1)
            waited = []

            async def during_wait(delay):
                waited.append(delay)
                assert member_mute_pending(CHAT, USER)
                if change in ("promoted", "left"):
                    bot.get_chat_member.return_value = SimpleNamespace(
                        status="administrator" if change == "promoted" else "left", is_member=change != "left"
                    )
                else:
                    async with session_scope() as session:
                        token = (await session.scalars(select(VerifyToken))).one()
                        if change == "expired":
                            token.expires_at = utc_now()-timedelta(seconds=1)
                        else:
                            token.used_at = utc_now()
                            state = await session.get(UserChatState, (CHAT, USER))
                            state.verified = True
                            state.verified_at = utc_now()

            monkeypatch.setattr(telegram_retry, "asyncio", SimpleNamespace(sleep=during_wait))
            await verify_flow._start_verification_for_member(bot, chat_id=CHAT, user_id=USER, chat_title="Audit")
            assert waited and waited[0] >= 1
            # The only permission request was before the state changed and was rate limited.
            bot.restrict_chat_member.assert_awaited_once()
            invite.assert_not_awaited()
            assert not member_mute_pending(CHAT, USER)
        finally:
            set_member_verification_exempt(CHAT, USER, False)
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["promoted", "departed"])
def test_membership_event_invalidates_retry_before_waiting_for_join_lock(monkeypatch, tmp_path, change):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        retirement = None
        try:
            method = RestrictChatMember(chat_id=CHAT, user_id=USER, permissions=muted_member_permissions())
            bot.restrict_chat_member.side_effect = TelegramRetryAfter(method=method, message="flood", retry_after=1)
            # Deliberately leave get_chat_member returning the old membership. The
            # newer chat_member update must invalidate work even before that RPC catches up.
            event = SimpleNamespace(
                chat=SimpleNamespace(id=CHAT, type="supergroup", title="Audit"),
                new_chat_member=SimpleNamespace(user=SimpleNamespace(id=USER, is_bot=False)),
            )

            async def during_wait(delay):
                nonlocal retirement
                callback = verify_flow.on_member_promoted if change == "promoted" else verify_flow.on_member_departed
                retirement = asyncio.create_task(callback(event, bot))
                await asyncio.sleep(0)
                assert not retirement.done()  # Waits for the in-progress join's member lock.
                if change == "promoted":
                    user_message = SimpleNamespace(
                        chat=event.chat, from_user=SimpleNamespace(id=USER, is_bot=False),
                    )
                    assert not await guest_bots._PendingVerificationMessageFilter()(user_message)

            monkeypatch.setattr(telegram_retry, "asyncio", SimpleNamespace(sleep=during_wait))
            await asyncio.wait_for(verify_flow._start_verification_for_member(
                bot, chat_id=CHAT, user_id=USER, chat_title="Audit",
            ), timeout=2)
            await asyncio.wait_for(retirement, timeout=2)
            bot.restrict_chat_member.assert_awaited_once()
            invite.assert_not_awaited()
            async with session_scope() as session:
                assert (await session.scalars(select(VerifyToken))).one().used_at is not None
                state = await session.get(UserChatState, (CHAT, USER))
                assert state.verified is (change == "promoted")
        finally:
            if retirement is not None and not retirement.done():
                retirement.cancel()
                await asyncio.gather(retirement, return_exceptions=True)
            set_member_verification_exempt(CHAT, USER, False)
            await engine.dispose()
    asyncio.run(run())


def test_batch_guards_every_human_before_first_database_await(monkeypatch, tmp_path):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        task = None
        try:
            original_scope = verify_flow.session_scope
            checked_first_await = False

            @asynccontextmanager
            async def observed_scope():
                nonlocal checked_first_await
                if not checked_first_await:
                    checked_first_await = True
                    assert member_mute_pending(CHAT, USER)
                    assert member_mute_pending(CHAT, USER+1)
                async with original_scope() as session:
                    yield session

            monkeypatch.setattr(verify_flow, "session_scope", observed_scope)
            first_waiting = asyncio.Event()
            release_first = asyncio.Event()

            async def slow_first_member(*args, **kwargs):
                if kwargs["user_id"] == USER:
                    first_waiting.set()
                    await release_first.wait()

            monkeypatch.setattr(verify_flow, "_start_verification_for_member", slow_first_member)
            members = [SimpleNamespace(id=uid, is_bot=False, language_code=None) for uid in (USER, USER+1)]
            message = SimpleNamespace(
                chat=SimpleNamespace(id=CHAT, type="supergroup", title="Audit"),
                new_chat_members=members, from_user=SimpleNamespace(id=USER, is_bot=False),
                message_thread_id=None, message_id=99,
            )
            task = asyncio.create_task(verify_flow.on_new_chat_members(message, bot))
            await asyncio.wait_for(first_waiting.wait(), timeout=1)
            second_message = SimpleNamespace(
                chat=message.chat, from_user=members[1], message_id=100,
            )
            assert await guest_bots._PendingVerificationMessageFilter()(second_message)
            await guest_bots.on_pending_verification_message(second_message, bot)
            bot.delete_message.assert_awaited_once_with(CHAT, 100)
            release_first.set()
            await asyncio.wait_for(task, timeout=1)
            assert checked_first_await
            assert not member_mute_pending(CHAT, USER)
            assert not member_mute_pending(CHAT, USER+1)
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())
