from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import GetChatMember
from sqlalchemy import select

from anticheatbot.db.models import UserChatState, VerifyToken
from anticheatbot.db.session import session_scope
from anticheatbot.handlers import guest_bots, verify_flow
from anticheatbot.services.groups import member_mute_pending, member_verification_exempt
from anticheatbot.time_utils import utc_now
from tests.test_join_event_idempotency import _events, _join
from tests.test_join_recovery_audit import CHAT, USER, _prepare


@pytest.mark.parametrize("branch", ["outer", "membership"])
def test_timeout_retry_respects_long_telegram_cooldown(monkeypatch, tmp_path, branch):
    async def run():
        engine, bot, _ = await _prepare(monkeypatch, tmp_path)
        try:
            error = TelegramRetryAfter(
                method=GetChatMember(chat_id=CHAT, user_id=USER), message="flood", retry_after=120,
            )
            if branch == "outer":
                monkeypatch.setattr(verify_flow, "_expire_verification", AsyncMock(side_effect=error))
            else:
                async with session_scope() as session:
                    session.add(VerifyToken(token="rate-limited", chat_id=CHAT, user_id=USER, expires_at=utc_now()-timedelta(seconds=1)))
                bot.get_chat_member.side_effect = error
            schedule = Mock()
            monkeypatch.setattr(verify_flow, "schedule_verification_timeout", schedule)
            await verify_flow._verify_timeout_task(
                bot, chat_id=CHAT, user_id=USER, token="rate-limited", delay=0, kick=True,
            )
            schedule.assert_called_once()
            assert schedule.call_args.kwargs["delay"] >= 120
            if branch == "membership":
                async with session_scope() as session:
                    assert (await session.get(VerifyToken, "rate-limited")).used_at is None
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("former", ["joined", "admin"])
def test_later_other_source_join_survives_missed_departure(monkeypatch, tmp_path, former):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        try:
            date = utc_now().replace(microsecond=0)
            event, notice = _events(date)
            await _join("member", event, notice, bot, 100)
            if former == "admin":
                event.new_chat_member.status = "administrator"
                await verify_flow.on_member_promoted(event, bot, event_update=SimpleNamespace(update_id=101))
            # Telegram may only deliver one kind of join notice after downtime. A different
            # source is not permanently tied to the previous lifecycle.
            later, later_notice = _events(date+timedelta(seconds=5))
            await _join("notice", later, later_notice, bot, 102)
            assert bot.restrict_chat_member.await_count == 2
            assert invite.await_count == 2
            async with session_scope() as session:
                assert len((await session.scalars(select(VerifyToken))).all()) == 2
                state = await session.get(UserChatState, (CHAT, USER))
                assert state.verified is False
                assert state.membership_status == "joined"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_old_join_waiting_for_inviter_cannot_delete_current_admin_message(monkeypatch, tmp_path):
    async def run():
        engine, bot, _ = await _prepare(monkeypatch, tmp_path)
        task = None
        try:
            date = utc_now().replace(microsecond=0)
            event, _ = _events(date)
            promoted = SimpleNamespace(
                chat=event.chat, date=date,
                new_chat_member=SimpleNamespace(status="administrator", user=event.new_chat_member.user),
            )
            await verify_flow.on_member_promoted(promoted, bot, event_update=SimpleNamespace(update_id=102))
            entered, release = asyncio.Event(), asyncio.Event()

            async def slow_inviter(*args):
                entered.set()
                await release.wait()
                return True

            monkeypatch.setattr(verify_flow, "_inviter_is_admin", slow_inviter)
            event.from_user = SimpleNamespace(id=99)
            task = asyncio.create_task(verify_flow.on_user_joined(event, bot, event_update=SimpleNamespace(update_id=100)))
            await asyncio.wait_for(entered.wait(), timeout=1)
            message = SimpleNamespace(chat=event.chat, from_user=event.new_chat_member.user, message_id=777)
            assert member_verification_exempt(CHAT, USER)
            should_delete = await guest_bots._PendingVerificationMessageFilter()(message)
            if should_delete:
                await guest_bots.on_pending_verification_message(message, bot)
            bot.delete_message.assert_not_awaited()
            release.set()
            await asyncio.wait_for(task, timeout=1)
            assert not member_mute_pending(CHAT, USER)
            assert (CHAT, USER) not in verify_flow._pending_join_events
            assert member_verification_exempt(CHAT, USER)
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())
