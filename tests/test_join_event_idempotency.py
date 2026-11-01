"""Cross-handler regressions for duplicate notices and real membership changes."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import RestrictChatMember
from sqlalchemy import select

from anticheatbot import telegram_retry
from anticheatbot.config import Settings
from anticheatbot.db.models import UserChatState, VerifyToken
from anticheatbot.db.session import session_scope
from anticheatbot.handlers import verify_flow
from anticheatbot.telegram_perms import muted_member_permissions
from anticheatbot.time_utils import utc_now
from anticheatbot.web import verify_api
from tests.signing import sign_webapp_init_data
from tests.test_join_recovery_audit import CHAT, USER, _prepare


def _events(date):
    user = SimpleNamespace(id=USER, is_bot=False, language_code="en")
    chat = SimpleNamespace(id=CHAT, type="supergroup", title="Audit")
    event = SimpleNamespace(
        chat=chat, date=date, from_user=user, via_join_request=False, invite_link=None,
        old_chat_member=SimpleNamespace(status="left", user=user),
        new_chat_member=SimpleNamespace(status="member", user=user),
    )
    notice = SimpleNamespace(
        chat=chat, date=date, from_user=user, new_chat_members=[user],
        message_id=444, message_thread_id=None,
    )
    return event, notice


async def _join(source, event, notice, bot, update_id):
    update = SimpleNamespace(update_id=update_id)
    if source == "member":
        await verify_flow.on_user_joined(event, bot, event_update=update)
    else:
        await verify_flow.on_new_chat_members(notice, bot, event_update=update)


def _request(bot, token):
    settings = Settings(BOT_TOKEN="12345:fake", WEBAPP_PUBLIC_URL="https://example.com")
    request = MagicMock()
    request.app = {"settings": settings, "bot": bot}
    request.headers = {
        "X-Telegram-Init-Data": sign_webapp_init_data(bot_token=settings.bot_token, user={"id": USER})
    }
    request.content_length = None
    request.json = AsyncMock(return_value={"token": token})
    request.remote = "127.0.0.1"
    return request


async def _live_token():
    async with session_scope() as session:
        return (await session.scalars(select(VerifyToken).where(VerifyToken.used_at.is_(None)))).one().token


@pytest.mark.parametrize("first", ["member", "notice"])
@pytest.mark.parametrize("clear_memory", [False, True])
def test_delayed_second_join_notice_cannot_remute_approved_member(monkeypatch, tmp_path, first, clear_memory):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        try:
            event, notice = _events(utc_now().replace(microsecond=0))
            await _join(first, event, notice, bot, 100)
            token = await _live_token()
            bot.get_chat_member.return_value = SimpleNamespace(status="restricted", is_member=True)
            assert (await verify_api.verify_rules_complete(_request(bot, token))).status == 200
            calls = bot.restrict_chat_member.await_count
            if clear_memory:
                # Recovery must use durable membership data, not the lifetime of a task's version object.
                verify_flow._membership_versions.clear()
                verify_flow._member_locks.clear()
            await _join("notice" if first == "member" else "member", event, notice, bot, 101)
            assert bot.restrict_chat_member.await_count == calls
            invite.assert_awaited_once()
            async with session_scope() as session:
                tokens = (await session.scalars(select(VerifyToken))).all()
                assert len(tokens) == 1 and tokens[0].used_at is not None
                assert (await session.get(UserChatState, (CHAT, USER))).verified is True
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("rejoin_seconds", [0, 1])
def test_real_departure_and_rapid_rejoin_need_a_new_token(monkeypatch, tmp_path, rejoin_seconds):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        try:
            date = utc_now().replace(microsecond=0)
            event, notice = _events(date)
            await _join("member", event, notice, bot, 100)
            old_token = await _live_token()
            bot.get_chat_member.return_value = SimpleNamespace(status="restricted", is_member=True)
            assert (await verify_api.verify_rules_complete(_request(bot, old_token))).status == 200
            departed = SimpleNamespace(
                chat=event.chat, date=date, old_chat_member=event.new_chat_member,
                new_chat_member=SimpleNamespace(status="left", user=event.new_chat_member.user),
            )
            await verify_flow.on_member_departed(departed, bot, event_update=SimpleNamespace(update_id=101))
            rejoin, rejoin_notice = _events(date + timedelta(seconds=rejoin_seconds))
            bot.get_chat_member.return_value = SimpleNamespace(status="member", is_member=True)
            calls = bot.restrict_chat_member.await_count
            await _join("member", rejoin, rejoin_notice, bot, 102)
            new_token = await _live_token()
            assert new_token != old_token
            assert bot.restrict_chat_member.await_count == calls + 1
            assert invite.await_count == 2
            with pytest.raises(web.HTTPForbidden):
                await verify_api.verify_rules_complete(_request(bot, old_token))
            async with session_scope() as session:
                assert (await session.get(UserChatState, (CHAT, USER))).verified is False
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_notice_from_previous_membership_arriving_after_new_approval_is_ignored(monkeypatch, tmp_path):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        try:
            date = utc_now().replace(microsecond=0)
            event, old_notice = _events(date)
            await _join("member", event, old_notice, bot, 100)
            departed = SimpleNamespace(
                chat=event.chat, date=date, old_chat_member=event.new_chat_member,
                new_chat_member=SimpleNamespace(status="left", user=event.new_chat_member.user),
            )
            await verify_flow.on_member_departed(departed, bot, event_update=SimpleNamespace(update_id=102))
            rejoin, new_notice = _events(date)
            await _join("member", rejoin, new_notice, bot, 103)
            new_token = await _live_token()
            bot.get_chat_member.return_value = SimpleNamespace(status="restricted", is_member=True)
            assert (await verify_api.verify_rules_complete(_request(bot, new_token))).status == 200
            calls = bot.restrict_chat_member.await_count
            # Telegram update 101 belonged to the first join, but processing was delayed.
            await _join("notice", event, old_notice, bot, 101)
            assert bot.restrict_chat_member.await_count == calls
            assert invite.await_count == 2
            async with session_scope() as session:
                assert len((await session.scalars(select(VerifyToken))).all()) == 2
                assert (await session.get(UserChatState, (CHAT, USER))).verified is True
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("old_status", ["left", "administrator"])
def test_stale_membership_event_cannot_cancel_new_join_retry(monkeypatch, tmp_path, old_status):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        retirement = None
        try:
            date = utc_now().replace(microsecond=0)
            event, notice = _events(date)
            await _join("member", event, notice, bot, 100)
            departed = SimpleNamespace(
                chat=event.chat, date=date, old_chat_member=event.new_chat_member,
                new_chat_member=SimpleNamespace(status="left", user=event.new_chat_member.user),
            )
            await verify_flow.on_member_departed(departed, bot, event_update=SimpleNamespace(update_id=102))
            bot.restrict_chat_member.reset_mock()
            method = RestrictChatMember(chat_id=CHAT, user_id=USER, permissions=muted_member_permissions())
            bot.restrict_chat_member.side_effect = [
                TelegramRetryAfter(method=method, message="flood", retry_after=1), True,
            ]
            old_event = SimpleNamespace(
                chat=event.chat, date=date, old_chat_member=event.new_chat_member,
                new_chat_member=SimpleNamespace(status=old_status, user=event.new_chat_member.user),
            )

            async def during_wait(delay):
                nonlocal retirement
                handler = verify_flow.on_member_departed if old_status == "left" else verify_flow.on_member_promoted
                retirement = asyncio.create_task(handler(
                    old_event, bot, event_update=SimpleNamespace(update_id=101),
                ))
                await asyncio.sleep(0)

            monkeypatch.setattr(telegram_retry, "asyncio", SimpleNamespace(sleep=during_wait))
            await asyncio.wait_for(_join("member", event, notice, bot, 103), timeout=2)
            await asyncio.wait_for(retirement, timeout=2)
            assert bot.restrict_chat_member.await_count == 2
            assert invite.await_count == 2
            assert await _live_token()
            async with session_scope() as session:
                assert (await session.get(UserChatState, (CHAT, USER))).verified is False
        finally:
            if retirement is not None and not retirement.done():
                retirement.cancel()
                await asyncio.gather(retirement, return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())
