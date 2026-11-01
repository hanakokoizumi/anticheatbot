from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import DeleteMessage
from sqlalchemy import select

from anticheatbot import telegram_retry
from anticheatbot.db.models import AuditEvent
from anticheatbot.db.session import configure_engine, session_scope
from anticheatbot.handlers import admin_cmds, guest_bots, root
from tests.test_bot_audit_regressions import _seed
from tests.test_guest_bot_race import _message


def _method():
    return DeleteMessage(chat_id=-100123, message_id=42)


def test_retry_waits_for_telegram_cooldown_and_remains_bounded(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(telegram_retry, "asyncio", SimpleNamespace(sleep=sleep))
    operation = AsyncMock(side_effect=[TelegramRetryAfter(method=_method(), message="wait", retry_after=2), True])
    assert asyncio.run(telegram_retry.retry_telegram(operation)) is True
    assert operation.await_count == 2
    assert sleep.call_args.args[0] >= 2
    operation = AsyncMock(side_effect=TelegramNetworkError(method=_method(), message="offline"))
    with pytest.raises(TelegramNetworkError):
        asyncio.run(telegram_retry.retry_telegram(operation))
    assert operation.await_count == 3


@pytest.mark.parametrize("error", [
    TelegramBadRequest(method=_method(), message="message cannot be deleted"),
    TelegramRetryAfter(method=_method(), message="wait", retry_after=120),
])
def test_permanent_or_very_long_failures_do_not_create_unbounded_retries(monkeypatch, error):
    sleep = AsyncMock()
    monkeypatch.setattr(telegram_retry, "asyncio", SimpleNamespace(sleep=sleep))
    operation = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        asyncio.run(telegram_retry.retry_telegram(operation))
    operation.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.parametrize("handler,event_name", [
    (guest_bots._delete_guest_bot_spam, "guest_bot_message_deleted"),
    (guest_bots.on_pending_verification_message, "unverified_message_deleted"),
])
def test_transient_delete_failure_retries_and_records_success(monkeypatch, handler, event_name):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _seed()
            monkeypatch.setattr(telegram_retry, "asyncio", SimpleNamespace(sleep=AsyncMock()))
            bot = SimpleNamespace(delete_message=AsyncMock(side_effect=[
                TelegramRetryAfter(method=_method(), message="wait", retry_after=1), True,
            ]))
            await handler(_message(user_id=55), bot) if handler == guest_bots.on_pending_verification_message else await handler(bot, _message(user_id=999, is_bot=True, caller_id=55))
            assert bot.delete_message.await_count == 2
            async with session_scope() as session:
                events = (await session.scalars(select(AuditEvent))).all()
                assert [event.event_type for event in events] == [event_name]
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_group_admin_link_opens_supported_private_webapp(monkeypatch):
    async def run():
        monkeypatch.setattr(admin_cmds, "_cached_bot_username", AsyncMock(return_value="testbot"))
        monkeypatch.setattr(admin_cmds, "get_settings", lambda: SimpleNamespace(webapp_public_url="https://example.com"))
        group = SimpleNamespace(chat=SimpleNamespace(id=-100123, type="supergroup"), from_user=SimpleNamespace(language_code="en"), answer=AsyncMock())
        await admin_cmds.cmd_admin_group(group, SimpleNamespace())
        button = group.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        assert button.web_app is None
        assert button.url == "https://t.me/testbot?start=admin_-100123"
        private = SimpleNamespace(chat=SimpleNamespace(id=55, type="private"), text="/start admin_-100123", from_user=SimpleNamespace(language_code="en"), answer=AsyncMock())
        await root.cmd_start(private)
        button = private.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        assert button.web_app.url == "https://example.com/admin/group.html?chat_id=-100123"
    asyncio.run(run())
