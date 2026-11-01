"""Edit eligibility follows the original review window, across restarts and joins."""
from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock
from weakref import WeakValueDictionary

import pytest
from alembic import command
from alembic.config import Config

from anticheatbot import llm
from anticheatbot.db.models import GroupSettings, UserChatState
from anticheatbot.db.session import configure_engine, session_scope
from anticheatbot.handlers import moderation
from anticheatbot.time_utils import utc_now
from tests.test_bot_audit_regressions import _moderation_settings, _seed, _text_message

CHAT, USER = -100123, 55


@asynccontextmanager
async def _scenario(monkeypatch, *, seen=0):
    engine = configure_engine("sqlite+aiosqlite:///:memory:")
    try:
        await _seed()
        async with session_scope() as session:
            session.add(UserChatState(
                chat_id=CHAT, user_id=USER, verified=True,
                verified_at=utc_now(), llm_messages_seen=seen,
            ))
        bot = _moderation_settings(monkeypatch)
        model = AsyncMock(return_value={"label": "ok", "confidence": 1})
        monkeypatch.setattr(moderation, "moderate_message", model)
        yield bot, model
    finally:
        await engine.dispose()


@pytest.mark.parametrize("seen", [0, 4, 5])
def test_unknown_old_edit_is_never_admitted(monkeypatch, seen):
    async def run():
        async with _scenario(monkeypatch, seen=seen) as (bot, model):
            model.return_value = {"label": "abuse", "confidence": 0.96}
            await moderation.on_edited_group_text(_text_message("old edited message"), bot)
            model.assert_not_awaited()
            bot.delete_message.assert_not_awaited()
            async with session_scope() as session:
                state = await session.get(UserChatState, (CHAT, USER))
                assert state.llm_messages_seen == seen
                assert state.llm_review_state == {}
    asyncio.run(run())


def test_unknown_edit_does_not_create_a_new_member_state(monkeypatch):
    async def run():
        async with _scenario(monkeypatch) as (bot, model):
            async with session_scope() as session:
                await session.delete(await session.get(UserChatState, (CHAT, USER)))
            await moderation.on_edited_group_text(_text_message("old edit"), bot)
            model.assert_not_awaited()
            async with session_scope() as session:
                assert await session.get(UserChatState, (CHAT, USER)) is None
    asyncio.run(run())


@pytest.mark.parametrize("caption", [False, True])
def test_only_first_five_messages_can_be_rechecked_after_restart(monkeypatch, caption):
    async def run():
        async with _scenario(monkeypatch) as (bot, model):
            for mid in range(42, 48):
                await moderation.on_group_text(_text_message("hello", mid=mid, caption=caption), bot)
            assert model.await_count == 5
            # Discard all process-local bookkeeping to exercise persistent eligibility.
            monkeypatch.setattr(moderation, "_message_versions", WeakValueDictionary())
            monkeypatch.setattr(moderation, "_moderation_locks", WeakValueDictionary())
            model.reset_mock()
            model.return_value = {"label": "spam", "confidence": 1}
            await moderation.on_edited_group_text(_text_message("spam", mid=47, caption=caption), bot)
            model.assert_not_awaited()
            bot.delete_message.assert_not_awaited()
            await moderation.on_edited_group_text(_text_message("spam", mid=46, caption=caption), bot)
            model.assert_awaited_once()
            bot.delete_message.assert_awaited_once_with(CHAT, 46)
            async with session_scope() as session:
                state = await session.get(UserChatState, (CHAT, USER))
                assert state.llm_messages_seen == 5
                assert state.llm_review_state["messages"] == {
                    str(mid): mid - 41 for mid in range(42, 47)
                }
    asyncio.run(run())


def test_rejoin_discards_old_message_eligibility(monkeypatch):
    async def run():
        async with _scenario(monkeypatch) as (bot, model):
            await moderation.on_group_text(_text_message("first membership"), bot)
            async with session_scope() as session:
                state = await session.get(UserChatState, (CHAT, USER))
                state.verified_at += timedelta(days=1)
                state.llm_messages_seen = 0
            model.reset_mock()
            await moderation.on_edited_group_text(_text_message("old edit"), bot)
            model.assert_not_awaited()
            await moderation.on_group_text(_text_message("new membership", mid=43), bot)
            await moderation.on_edited_group_text(_text_message("new edit", mid=43), bot)
            assert model.await_count == 2
            async with session_scope() as session:
                state = await session.get(UserChatState, (CHAT, USER))
                assert state.llm_messages_seen == 1
                assert state.llm_review_state["messages"] == {"43": 1}
    asyncio.run(run())


def test_lowered_quota_limits_which_edits_can_be_rechecked(monkeypatch):
    async def run():
        async with _scenario(monkeypatch) as (bot, model):
            for mid in range(42, 45):
                await moderation.on_group_text(_text_message(mid=mid), bot)
            async with session_scope() as session:
                (await session.get(GroupSettings, CHAT)).llm_max_messages = 2
            model.reset_mock()
            await moderation.on_edited_group_text(_text_message(mid=44), bot)
            model.assert_not_awaited()
            await moderation.on_edited_group_text(_text_message(mid=43), bot)
            model.assert_awaited_once()
    asyncio.run(run())


def test_failed_review_releases_its_reservation(monkeypatch):
    async def run():
        async with _scenario(monkeypatch, seen=4) as (bot, model):
            model.side_effect = [llm.LLMError("timeout"), {"label": "ok", "confidence": 1}]
            await moderation.on_group_text(_text_message("timeout"), bot)
            async with session_scope() as session:
                state = await session.get(UserChatState, (CHAT, USER))
                assert state.llm_messages_seen == 4
                assert state.llm_review_state["messages"] == {}
            await moderation.on_group_text(_text_message("retry slot", mid=43), bot)
            async with session_scope() as session:
                state = await session.get(UserChatState, (CHAT, USER))
                assert state.llm_messages_seen == 5
                assert state.llm_review_state["messages"] == {"43": 5}
    asyncio.run(run())


@pytest.mark.parametrize("before_lock", [False, True])
def test_edit_replacing_original_consumes_the_last_slot_once(monkeypatch, before_lock):
    async def run():
        async with _scenario(monkeypatch, seen=4) as (bot, model):
            entered, edit_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
            real_review = moderation._review_group_message

            async def observe(message, bot, *, is_edit):
                (edit_entered if is_edit else entered).set()
                return await real_review(message, bot, is_edit=is_edit)

            async def verdict(**kwargs):
                if kwargs["text"] == "original":
                    entered.set()
                    await release.wait()
                    return {"label": "ok", "confidence": 1}
                return {"label": "spam", "confidence": 1}

            if before_lock:
                monkeypatch.setattr(moderation, "_review_group_message", observe)
                lock = asyncio.Lock()
                moderation._moderation_locks[(CHAT, USER)] = lock
                await lock.acquire()
            model.side_effect = verdict
            original = asyncio.create_task(moderation.on_group_text(_text_message("original"), bot))
            await asyncio.wait_for(entered.wait(), timeout=2)
            monkeypatch.setattr(moderation, "_review_group_message", observe)
            edited = asyncio.create_task(moderation.on_edited_group_text(_text_message("edited"), bot))
            await asyncio.wait_for(edit_entered.wait(), timeout=2)
            if before_lock:
                lock.release()
            release.set()
            await asyncio.wait_for(asyncio.gather(original, edited), timeout=2)
            assert model.await_count == (1 if before_lock else 2)
            bot.delete_message.assert_awaited_once_with(CHAT, 42)
            async with session_scope() as session:
                state = await session.get(UserChatState, (CHAT, USER))
                assert state.llm_messages_seen == 5
                assert state.llm_review_state["messages"] == {"42": 5}
            model.reset_mock()
            await moderation.on_group_text(_text_message("sixth", mid=43), bot)
            await moderation.on_edited_group_text(_text_message("sixth edit", mid=43), bot)
            model.assert_not_awaited()
    asyncio.run(run())


def test_migration_preserves_legacy_member_quota(monkeypatch, tmp_path):
    database = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    config = Config("alembic.ini")
    command.upgrade(config, "0007_member_lifecycle")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO user_chat_states (chat_id, user_id, verified, llm_messages_seen) VALUES (?, ?, ?, ?)",
            (CHAT, USER, 1, 5),
        )
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT llm_messages_seen, llm_review_state FROM user_chat_states WHERE chat_id = ? AND user_id = ?",
            (CHAT, USER),
        ).fetchone()
        assert row == (5, "{}")
    command.downgrade(config, "0007_member_lifecycle")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT llm_messages_seen FROM user_chat_states").fetchone() == (5,)
