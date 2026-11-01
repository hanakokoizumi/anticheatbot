from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetChatMember, SendMessage
from sqlalchemy import event, func, select

from anticheatbot.db.models import GroupSettings, KnownChat, UserChatState, VerifyToken
from anticheatbot.db.session import configure_engine, create_all, session_scope
from anticheatbot.handlers import moderation, verify_flow
from anticheatbot.services.groups import get_or_create_group_settings, touch_known_chat
from anticheatbot.time_utils import utc_now
from tests.test_bot_audit_regressions import _moderation_settings, _seed, _text_message


@pytest.mark.parametrize("action", ["delete", "delete_and_restrict"])
def test_moderation_honors_configured_action(monkeypatch, action):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _seed()
            async with session_scope() as session:
                (await session.get(GroupSettings, -100123)).spam_action = action
            bot = _moderation_settings(monkeypatch)
            bot.restrict_chat_member = AsyncMock()
            monkeypatch.setattr(moderation, "moderate_message", AsyncMock(return_value={"label": "spam", "confidence": 1}))
            await moderation.on_group_text(_text_message("spam"), bot)
            bot.delete_message.assert_awaited_once()
            assert bot.restrict_chat_member.await_count == (1 if action == "delete_and_restrict" else 0)
            if action == "delete_and_restrict":
                assert bot.restrict_chat_member.call_args.kwargs["permissions"].can_send_messages is False
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["disabled", "rejoined", "threshold", "promoted"])
def test_slow_review_rechecks_current_policy_and_membership(monkeypatch, change):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _seed()
            bot = _moderation_settings(monkeypatch)
            async def model(**kwargs):
                async with session_scope() as session:
                    gs = await session.get(GroupSettings, -100123)
                    state = await session.get(UserChatState, (-100123, 55))
                    if change == "disabled":
                        gs.llm_enabled = False
                    elif change == "rejoined":
                        state.verified = False
                        state.verified_at = None
                    elif change == "threshold":
                        gs.llm_min_confidence_action = 1
                    else:
                        bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
                return {"label": "spam", "confidence": 0.9}
            monkeypatch.setattr(moderation, "moderate_message", model)
            await moderation.on_group_text(_text_message("spam"), bot)
            bot.delete_message.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_zero_review_limit_also_disables_edit_reviews(monkeypatch):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _seed()
            async with session_scope() as session:
                (await session.get(GroupSettings, -100123)).llm_max_messages = 0
            bot = _moderation_settings(monkeypatch)
            model = AsyncMock()
            monkeypatch.setattr(moderation, "moderate_message", model)
            await moderation.on_edited_group_text(_text_message("spam"), bot)
            model.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_first_group_updates_can_create_same_rows_concurrently(tmp_path):
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/groups.sqlite")
        try:
            await create_all()
            async def first_update():
                async with session_scope() as session:
                    await touch_known_chat(session, -100123, "Group")
                    group = await get_or_create_group_settings(session, -100123)
                    assert group.verification_enabled is True
            await asyncio.gather(first_update(), first_update())
            async with session_scope() as session:
                assert await session.scalar(select(func.count()).select_from(KnownChat)) == 1
                assert await session.scalar(select(func.count()).select_from(GroupSettings)) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_bot_and_member_first_join_create_group_in_one_lock_order(monkeypatch, tmp_path):
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/first-joins.sqlite")
        try:
            await create_all()
            order_by_session = {}
            real_touch = verify_flow.touch_known_chat
            real_group = verify_flow.get_or_create_group_settings

            async def touch(session, *args, **kwargs):
                order_by_session.setdefault(id(session), []).append("known_chat")
                return await real_touch(session, *args, **kwargs)

            async def group(session, *args, **kwargs):
                order_by_session.setdefault(id(session), []).append("group_settings")
                return await real_group(session, *args, **kwargs)

            monkeypatch.setattr(verify_flow, "touch_known_chat", touch)
            monkeypatch.setattr(verify_flow, "get_or_create_group_settings", group)
            monkeypatch.setattr(verify_flow, "get_settings", lambda: SimpleNamespace(global_admin_id_set=set()))
            monkeypatch.setattr(verify_flow, "_send_verify_invite_message", AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=-100123), message_id=77)))
            monkeypatch.setattr(verify_flow, "schedule_verification_timeout", Mock())
            bot = SimpleNamespace(id=123, restrict_chat_member=AsyncMock(), get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")))
            event = SimpleNamespace(
                chat=SimpleNamespace(id=-100123, type="supergroup", title="Group"),
                new_chat_member=SimpleNamespace(user=SimpleNamespace(id=123), status="administrator"),
            )
            await asyncio.wait_for(asyncio.gather(
                verify_flow.on_bot_chat_member(event, bot),
                verify_flow._start_verification_for_member(bot, chat_id=-100123, user_id=55, chat_title="Group"),
            ), timeout=5)
            # This order is the shared PostgreSQL lock invariant, independent of SQLite's write serialization.
            for calls in order_by_session.values():
                assert calls[:2] == ["known_chat", "group_settings"]
            async with session_scope() as session:
                assert await session.scalar(select(func.count()).select_from(KnownChat)) == 1
                assert await session.scalar(select(func.count()).select_from(GroupSettings)) == 1
                assert await session.scalar(select(func.count()).select_from(VerifyToken)) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_existing_unchanged_group_touch_performs_no_writes():
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _seed()
            statements = []
            def capture(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement.lstrip().upper())
            event.listen(engine.sync_engine, "before_cursor_execute", capture)
            async with session_scope() as session:
                await touch_known_chat(session, -100123, "Test")
                await get_or_create_group_settings(session, -100123)
            assert statements and all(statement.startswith("SELECT") for statement in statements)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_disabled_verification_join_clears_stale_unverified_state(monkeypatch):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _seed()
            async with session_scope() as session:
                (await session.get(GroupSettings, -100123)).verification_enabled = False
                session.add(UserChatState(chat_id=-100123, user_id=55, verified=False, llm_messages_seen=5))
            monkeypatch.setattr(verify_flow, "get_settings", lambda: SimpleNamespace(global_admin_id_set=set()))
            await verify_flow._start_verification_for_member(SimpleNamespace(), chat_id=-100123, user_id=55, chat_title="Group")
            async with session_scope() as session:
                state = await session.get(UserChatState, (-100123, 55))
                assert state.verified is True
                assert state.llm_messages_seen == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("missing", [False, True])
def test_expired_token_releases_disabled_verification_or_retires_missing_member(monkeypatch, missing):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _seed()
            async with session_scope() as session:
                (await session.get(GroupSettings, -100123)).verification_enabled = missing
                session.add(UserChatState(chat_id=-100123, user_id=55, verified=False))
                session.add(VerifyToken(token="old", chat_id=-100123, user_id=55, expires_at=utc_now()-timedelta(minutes=1)))
            bot = SimpleNamespace(
                get_chat_member=AsyncMock(return_value=SimpleNamespace(status="restricted", is_member=True)),
                restrict_chat_member=AsyncMock(), ban_chat_member=AsyncMock(),
            )
            if missing:
                bot.get_chat_member.side_effect = TelegramBadRequest(method=GetChatMember(chat_id=-100123, user_id=55), message="Bad Request: member not found")
            schedule = Mock()
            monkeypatch.setattr(verify_flow, "schedule_verification_timeout", schedule)
            await verify_flow._verify_timeout_task(bot, chat_id=-100123, user_id=55, token="old", delay=0, kick=True)
            schedule.assert_not_called()
            bot.ban_chat_member.assert_not_awaited()
            assert bot.restrict_chat_member.await_count == (0 if missing else 1)
            async with session_scope() as session:
                assert (await session.get(VerifyToken, "old")).used_at is not None
                if not missing:
                    assert (await session.get(UserChatState, (-100123, 55))).verified is True
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_welcome_network_failure_does_not_turn_success_into_failure():
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(side_effect=TelegramNetworkError(method=GetChatMember(chat_id=-100123, user_id=55), message="offline")),
        send_message=AsyncMock(side_effect=TelegramNetworkError(method=SendMessage(chat_id=-100123, text="welcome"), message="offline")),
    )
    asyncio.run(verify_flow.send_post_verify_welcome(bot, chat_id=-100123, user_id=55, template="Hi {mention}"))
    bot.send_message.assert_awaited_once()
