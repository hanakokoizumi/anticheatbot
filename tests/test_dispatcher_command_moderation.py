"""Exercise real aiogram routing, including command short-circuit behavior."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import DeleteMessage
from aiogram.types import Update

from anticheatbot.db.models import GroupSettings, KnownChat, UserChatState, VerifyToken
from anticheatbot.db.session import configure_engine, create_all, session_scope
from anticheatbot.handlers import admin_cmds, guest_bots, moderation, setup_handlers
from anticheatbot.services.groups import set_guest_bot_block_enabled
from anticheatbot.time_utils import utc_now

CHAT, USER = -100123, 55


@asynccontextmanager
async def _dispatcher(monkeypatch):
    engine = configure_engine("sqlite+aiosqlite:///:memory:")
    bot = Bot("12345:fake")
    original_session = bot.session
    dp = Dispatcher()
    journal = []
    try:
        await create_all()
        async with session_scope() as session:
            session.add(KnownChat(chat_id=CHAT, title="Audit"))
            session.add(GroupSettings(chat_id=CHAT, llm_enabled=True, llm_max_messages=5))
            session.add(UserChatState(chat_id=CHAT, user_id=USER, verified=True, verified_at=utc_now()))
        settings = SimpleNamespace(
            global_admin_id_set=set(), openai_api_key="test", openai_base_url="https://example.test",
            effective_moderation_model="gpt-6-luna", webapp_public_url="https://example.test",
        )
        for module in (moderation, guest_bots, admin_cmds):
            monkeypatch.setattr(module, "get_settings", lambda: settings)
        monkeypatch.setattr(admin_cmds, "_cached_bot_username", AsyncMock(return_value="test_bot"))
        monkeypatch.setattr(bot, "get_chat_member", AsyncMock(return_value=SimpleNamespace(status="member")))

        async def send(*args, **kwargs):
            journal.append("reply")
            return True

        async def delete(*args, **kwargs):
            journal.append("delete")
            return True

        monkeypatch.setattr(bot, "session", AsyncMock(side_effect=send))
        monkeypatch.setattr(bot, "delete_message", AsyncMock(side_effect=delete))
        model = AsyncMock(return_value={"label": "ok", "confidence": 1})

        async def review(*args, **kwargs):
            journal.append("review")
            return model.return_value

        model.side_effect = review
        monkeypatch.setattr(moderation, "moderate_message", model)
        setup_handlers(dp)
        yield SimpleNamespace(dp=dp, bot=bot, model=model, journal=journal, settings=settings)
    finally:
        # Module routers are singletons; detach them for the next real Dispatcher fixture.
        for router in dp.sub_routers:
            router._parent_router = None
        dp.sub_routers.clear()
        set_guest_bot_block_enabled(CHAT, False)
        await dp.storage.close()
        await original_session.close()
        await engine.dispose()


def _update(command="/start", *, private=False, guest=False, edited=False):
    user = {"id": 999 if guest else USER, "is_bot": guest, "first_name": "Audit"}
    message = {
        "message_id": 42, "date": 1, "chat": {"id": USER if private else CHAT, "type": "private" if private else "supergroup"},
        "from": user, "text": command + " @spamOneBot @spamTwoBot BUY NOW",
        "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
    }
    if guest:
        message["guest_bot_caller_user"] = {"id": USER, "is_bot": False, "first_name": "Caller"}
    if edited:
        message["edit_date"] = 2
    return Update.model_validate({"update_id": 1, "edited_message" if edited else "message": message})


async def _seen():
    async with session_scope() as session:
        return (await session.get(UserChatState, (CHAT, USER))).llm_messages_seen


@pytest.mark.parametrize("command", ["/start", "/admin"])
@pytest.mark.parametrize("delete_fails", [False, True])
def test_spam_command_is_reviewed_once_and_never_answered(monkeypatch, command, delete_fails):
    async def run():
        async with _dispatcher(monkeypatch) as ctx:
            ctx.model.return_value = {"label": "spam", "confidence": 1}
            if delete_fails:
                ctx.bot.delete_message.side_effect = TelegramForbiddenError(
                    method=DeleteMessage(chat_id=CHAT, message_id=42), message="permission removed",
                )
            await ctx.dp.feed_update(ctx.bot, _update(command))
            ctx.model.assert_awaited_once()
            ctx.bot.delete_message.assert_awaited_once_with(CHAT, 42)
            ctx.bot.session.assert_not_awaited()
            assert await _seen() == (0 if delete_fails else 1)
    asyncio.run(run())


@pytest.mark.parametrize("command", ["/start", "/admin"])
@pytest.mark.parametrize("private", [False, True])
def test_allowed_commands_still_reply_after_at_most_one_review(monkeypatch, command, private):
    async def run():
        async with _dispatcher(monkeypatch) as ctx:
            await ctx.dp.feed_update(ctx.bot, _update(command, private=private))
            assert ctx.journal == (["reply"] if private else ["review", "reply"])
            assert ctx.model.await_count == int(not private)
            assert await _seen() == int(not private)
            ctx.bot.delete_message.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("global_admin", [False, True])
def test_admin_command_bypass_is_preserved(monkeypatch, global_admin):
    async def run():
        async with _dispatcher(monkeypatch) as ctx:
            if global_admin:
                ctx.settings.global_admin_id_set.add(USER)
            else:
                ctx.bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
            await ctx.dp.feed_update(ctx.bot, _update("/admin"))
            assert ctx.journal == ["reply"]
            ctx.model.assert_not_awaited()
            assert await _seen() == 0
    asyncio.run(run())


@pytest.mark.parametrize("command", ["/start", "/admin"])
def test_pending_member_command_is_deleted_before_review_or_reply(monkeypatch, command):
    async def run():
        async with _dispatcher(monkeypatch) as ctx:
            async with session_scope() as session:
                (await session.get(UserChatState, (CHAT, USER))).verified = False
                session.add(VerifyToken(token="pending-test", chat_id=CHAT, user_id=USER, expires_at=utc_now()+timedelta(minutes=1)))
            await ctx.dp.feed_update(ctx.bot, _update(command))
            assert ctx.journal == ["delete"]
            ctx.model.assert_not_awaited()
            assert await _seen() == 0
    asyncio.run(run())


@pytest.mark.parametrize("edited", [False, True])
def test_blocked_guest_deletes_before_database_review_or_command(monkeypatch, edited):
    async def run():
        async with _dispatcher(monkeypatch) as ctx:
            set_guest_bot_block_enabled(CHAT, True)
            async with session_scope() as session:
                (await session.get(GroupSettings, CHAT)).verification_enabled = False

            @asynccontextmanager
            async def audit_only_after_delete():
                assert ctx.journal == ["delete"]
                async with session_scope() as session:
                    yield session

            monkeypatch.setattr(guest_bots, "session_scope", audit_only_after_delete)
            await ctx.dp.feed_update(ctx.bot, _update(guest=True, edited=edited))
            assert ctx.journal == ["delete"]
            ctx.model.assert_not_awaited()
            ctx.bot.session.assert_not_awaited()
    asyncio.run(run())


def test_edited_command_cannot_receive_a_late_reply_from_its_old_review(monkeypatch):
    async def run():
        async with _dispatcher(monkeypatch) as ctx:
            entered, release, edit_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
            original = edited = None

            async def review(*args, **kwargs):
                if ctx.model.await_count == 1:
                    entered.set()
                    await release.wait()
                    return {"label": "ok", "confidence": 1}
                return {"label": "spam", "confidence": 1}

            real_review = moderation._review_group_message

            async def observe_edit(message, bot, *, is_edit):
                if is_edit:
                    edit_entered.set()
                return await real_review(message, bot, is_edit=is_edit)

            ctx.model.side_effect = review
            monkeypatch.setattr(moderation, "_review_group_message", observe_edit)
            try:
                original = asyncio.create_task(ctx.dp.feed_update(ctx.bot, _update("/start")))
                await asyncio.wait_for(entered.wait(), timeout=2)
                edited = asyncio.create_task(ctx.dp.feed_update(ctx.bot, _update("/start", edited=True)))
                await asyncio.wait_for(edit_entered.wait(), timeout=2)
                release.set()
                await asyncio.wait_for(asyncio.gather(original, edited), timeout=2)
                assert ctx.model.await_count == 2
                ctx.bot.delete_message.assert_awaited_once_with(CHAT, 42)
                ctx.bot.session.assert_not_awaited()
                assert await _seen() == 1
            finally:
                release.set()
                for task in (original, edited):
                    if task is not None and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
