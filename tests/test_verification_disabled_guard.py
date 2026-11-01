from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from anticheatbot.db.models import GroupSettings, VerifyToken
from anticheatbot.db.session import session_scope
from anticheatbot.handlers import guest_bots, verify_flow
from anticheatbot.services.groups import set_guest_bot_block_enabled
from anticheatbot.time_utils import utc_now
from tests.test_join_event_idempotency import _events, _join
from tests.test_join_recovery_audit import CHAT, USER, _prepare


def _message(*, guest=False):
    return SimpleNamespace(
        chat=SimpleNamespace(id=CHAT, type="supergroup"), message_id=777,
        from_user=SimpleNamespace(id=999 if guest else USER, is_bot=guest),
        guest_bot_caller_user=SimpleNamespace(id=USER) if guest else None,
        guest_bot_caller_chat=None,
    )


@pytest.mark.parametrize("source", ["member", "notice"])
@pytest.mark.parametrize("enabled", [False, True])
def test_early_pending_guard_honors_current_group_switch(monkeypatch, tmp_path, source, enabled):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        task = None
        release = asyncio.Event()
        try:
            async with session_scope() as session:
                (await session.get(GroupSettings, CHAT)).verification_enabled = enabled
            event, notice = _events(utc_now().replace(microsecond=0))
            event.from_user = notice.from_user = SimpleNamespace(id=99, is_bot=False)
            entered = asyncio.Event()

            async def slow_inviter(*args):
                entered.set()
                await release.wait()
                return False

            monkeypatch.setattr(verify_flow, "_inviter_is_admin", slow_inviter)
            task = asyncio.create_task(_join(source, event, notice, bot, 100))
            await asyncio.wait_for(entered.wait(), timeout=1)
            message = _message()
            intercepted = await guest_bots._PendingVerificationMessageFilter()(message)
            assert intercepted is enabled
            if intercepted:
                await guest_bots.on_pending_verification_message(message, bot)
            assert bot.delete_message.await_count == int(enabled)
            release.set()
            await asyncio.wait_for(task, timeout=1)
            assert invite.await_count == int(enabled)
            assert bot.restrict_chat_member.await_count == int(enabled)
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())


def test_disabling_verification_stops_old_token_message_guard_immediately(monkeypatch, tmp_path):
    async def run():
        engine, _, _ = await _prepare(monkeypatch, tmp_path)
        set_guest_bot_block_enabled(CHAT, False)
        try:
            async with session_scope() as session:
                session.add(VerifyToken(token="old-live", chat_id=CHAT, user_id=USER, expires_at=utc_now()+timedelta(minutes=2)))
            assert await guest_bots._PendingVerificationMessageFilter()(_message())
            assert await guest_bots._BlockGuestBotsInGroupFilter()(_message(guest=True))
            async with session_scope() as session:
                (await session.get(GroupSettings, CHAT)).verification_enabled = False
            assert not await guest_bots._PendingVerificationMessageFilter()(_message())
            assert not await guest_bots._BlockGuestBotsInGroupFilter()(_message(guest=True))
            async with session_scope() as session:
                assert (await session.get(VerifyToken, "old-live")).used_at is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_guest_block_still_deletes_first_when_verification_is_disabled(monkeypatch, tmp_path):
    async def run():
        engine, bot, _ = await _prepare(monkeypatch, tmp_path)
        try:
            async with session_scope() as session:
                group = await session.get(GroupSettings, CHAT)
                group.verification_enabled = False
                group.block_guest_bots = True
            set_guest_bot_block_enabled(CHAT, True)
            order = []

            async def delete(*args):
                order.append("delete")

            @asynccontextmanager
            async def audit_after_delete():
                assert order == ["delete"]
                order.append("audit")
                async with session_scope() as session:
                    yield session

            bot.delete_message = AsyncMock(side_effect=delete)
            monkeypatch.setattr(guest_bots, "session_scope", audit_after_delete)
            message = _message(guest=True)
            assert await guest_bots._BlockGuestBotsInGroupFilter()(message)
            assert order == []
            await guest_bots.on_group_message_guest_context(message, bot)
            assert order == ["delete", "audit"]
            bot.delete_message.assert_awaited_once_with(CHAT, 777)
        finally:
            set_guest_bot_block_enabled(CHAT, False)
            await engine.dispose()
    asyncio.run(run())
