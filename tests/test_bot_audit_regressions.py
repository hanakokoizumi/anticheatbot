from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from weakref import WeakValueDictionary

import httpx
import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import RestrictChatMember, SendMessage
from aiogram.types import Message
from sqlalchemy import select

from anticheatbot import llm
from anticheatbot.db.models import GroupSettings, KnownChat, UserChatState, VerifyToken
from anticheatbot.db.session import configure_engine, create_all, session_scope
from anticheatbot.handlers import guest_bots, moderation, verify_flow
from anticheatbot.time_utils import utc_now


@pytest.mark.parametrize("verdict", [
    {"label": "unknown", "confidence": 1},
    {"label": "spam", "confidence": float("nan")},
    {"label": "spam", "confidence": float("inf")},
    {"label": "spam", "confidence": True},
    {"label": "spam", "confidence": "0.9"},
    {"label": "spam", "confidence": 2},
    {"label": "spam", "confidence": -1},
])
def test_invalid_verdict_cannot_trigger_deletion(monkeypatch, verdict) -> None:
    monkeypatch.setattr(llm, "chat_completion_json", AsyncMock(return_value=verdict))
    with pytest.raises(llm.LLMError, match="invalid moderation verdict"):
        asyncio.run(llm.moderate_message(base_url="https://example.test", api_key="test", model="test", text="hi"))


@pytest.mark.parametrize("kind", ["timeout", "invalid_json", "missing_choices", "non_object", "null_content"])
def test_provider_failures_are_llm_errors(monkeypatch, kind) -> None:
    real_client = httpx.AsyncClient

    def reply(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if kind == "invalid_json":
            return httpx.Response(200, text="not json")
        if kind == "missing_choices":
            return httpx.Response(200, json={})
        content = "[]" if kind == "non_object" else None
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(reply), **kw))
    with pytest.raises(llm.LLMError):
        asyncio.run(llm.chat_completion_json(base_url="https://example.test", api_key="test", model="test", messages=[]))


def test_group_invite_uses_supported_direct_link_first(monkeypatch) -> None:
    monkeypatch.setattr(verify_flow, "_cached_bot_username", AsyncMock(return_value="testbot"))
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(verify_flow._send_verify_invite_message(
        bot, target_chat_id=-100123, text="verify", settings=SimpleNamespace(webapp_public_url="https://example.test"),
        token="token", loc="en", message_thread_id=None, channel_label="group",
    ))
    bot.send_message.assert_awaited_once()
    button = bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.url == "https://t.me/testbot?startapp=token"
    assert button.web_app is None


@pytest.mark.parametrize(("status", "expected"), [("member", False), ("administrator", True), ("creator", True)])
def test_direct_add_bypass_requires_actual_admin(monkeypatch, status, expected) -> None:
    starter = AsyncMock()
    monkeypatch.setattr(verify_flow, "_start_verification_for_member", starter)
    user = SimpleNamespace(id=55, is_bot=False, language_code=None)
    event = SimpleNamespace(
        chat=SimpleNamespace(id=-100123, title="Test"), new_chat_member=SimpleNamespace(user=user),
        from_user=SimpleNamespace(id=99), via_join_request=False, invite_link=None,
    )
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status=status)))
    asyncio.run(verify_flow.on_user_joined(event, bot))
    assert starter.call_args.kwargs["direct_admin_add"] is expected


async def _seed() -> None:
    await create_all()
    async with session_scope() as session:
        session.add(KnownChat(chat_id=-100123, title="Test"))
        session.add(GroupSettings(chat_id=-100123, verification_enabled=True))


def test_join_pair_race_creates_one_token_and_resets_old_verification(monkeypatch) -> None:
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await _seed()
        async with session_scope() as session:
            session.add(UserChatState(chat_id=-100123, user_id=55, verified=True, llm_messages_seen=5))
        monkeypatch.setattr(verify_flow, "get_settings", lambda: SimpleNamespace(global_admin_id_set=set()))
        monkeypatch.setattr(verify_flow, "_send_verify_invite_message", AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=-100123), message_id=77)))
        monkeypatch.setattr(verify_flow, "schedule_verification_timeout", Mock())
        bot = SimpleNamespace(restrict_chat_member=AsyncMock(), get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")))
        await asyncio.gather(*[
            verify_flow._start_verification_for_member(bot, chat_id=-100123, user_id=55, chat_title="Test")
            for _ in range(2)
        ])
        bot.restrict_chat_member.assert_awaited_once()
        async with session_scope() as session:
            assert len((await session.scalars(select(VerifyToken))).all()) == 1
            state = await session.get(UserChatState, (-100123, 55))
            assert state.verified is False
            assert state.llm_messages_seen == 0
        await engine.dispose()
    asyncio.run(run())


def test_pending_sticker_is_deleted(monkeypatch) -> None:
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await _seed()
        monkeypatch.setattr(guest_bots, "get_settings", lambda: SimpleNamespace(global_admin_id_set=set()))
        async with session_scope() as session:
            session.add(VerifyToken(token="live", chat_id=-100123, user_id=55, expires_at=utc_now() + timedelta(minutes=2)))
        msg = Message.model_validate({
            "message_id": 42, "date": 1, "chat": {"id": -100123, "type": "supergroup"},
            "from": {"id": 55, "is_bot": False, "first_name": "Test"},
            "sticker": {"file_id": "a", "file_unique_id": "b", "type": "regular", "width": 10, "height": 10, "is_animated": False, "is_video": False},
        })
        bot = SimpleNamespace(delete_message=AsyncMock())
        await guest_bots.router.message.trigger(msg, bot=bot)
        bot.delete_message.assert_awaited_once_with(-100123, 42)
        await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize(("verified", "member_status", "expected_kicks"), [
    (False, "restricted", 1), (True, "restricted", 0), (False, "kicked", 0),
    (False, "left", 0), (False, "administrator", 0), (False, "creator", 0),
])
def test_expired_token_consumption_checks_current_membership(verified, member_status, expected_kicks) -> None:
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await _seed()
        async with session_scope() as session:
            session.add(UserChatState(chat_id=-100123, user_id=55, verified=verified))
            session.add(VerifyToken(token="expired", chat_id=-100123, user_id=55, expires_at=utc_now() - timedelta(seconds=1)))
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(), unban_chat_member=AsyncMock(),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status=member_status, is_member=True)),
        )
        await asyncio.gather(*[
            verify_flow._verify_timeout_task(bot, chat_id=-100123, user_id=55, token="expired", delay=0, kick=True)
            for _ in range(2)
        ])
        assert bot.ban_chat_member.await_count == expected_kicks
        async with session_scope() as session:
            assert (await session.get(VerifyToken, "expired")).used_at is not None
        await engine.dispose()
    asyncio.run(run())


def test_recovery_only_schedules_latest_unfinished_membership(monkeypatch) -> None:
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await _seed()
        now = utc_now()
        async with session_scope() as session:
            session.add_all([
                VerifyToken(token="old", chat_id=-100123, user_id=55, created_at=now-timedelta(minutes=3), expires_at=now-timedelta(minutes=2)),
                VerifyToken(token="used", chat_id=-100123, user_id=55, created_at=now-timedelta(minutes=1), expires_at=now+timedelta(minutes=2), used_at=now),
                VerifyToken(token="pending", chat_id=-100123, user_id=66, created_at=now, expires_at=now+timedelta(minutes=2)),
            ])
        schedule = Mock()
        monkeypatch.setattr(verify_flow, "schedule_verification_timeout", schedule)
        assert await verify_flow.restore_verification_timeouts(SimpleNamespace()) == 1
        assert schedule.call_args.kwargs["token"] == "pending"
        await engine.dispose()
    asyncio.run(run())


def _text_message(text="hi", mid=42, *, caption=False):
    payload = {
        "message_id": mid, "date": 1, "chat": {"id": -100123, "type": "supergroup"},
        "from": {"id": 55, "is_bot": False, "first_name": "Test"},
    }
    payload["caption" if caption else "text"] = text
    return Message.model_validate(payload)


def _moderation_settings(monkeypatch):
    # Each test starts its own asyncio.run loop, unlike the single production loop.
    # Completed tasks may retain weak-map locks until GC, especially on Python 3.12.
    monkeypatch.setattr(moderation, "_moderation_locks", WeakValueDictionary())
    monkeypatch.setattr(moderation, "_message_versions", WeakValueDictionary())
    monkeypatch.setattr(moderation, "_moderation_semaphore", asyncio.Semaphore(8))
    monkeypatch.setattr(moderation, "get_settings", lambda: SimpleNamespace(
        global_admin_id_set=set(), openai_api_key="test", openai_base_url="https://example.test",
        effective_moderation_model="test",
    ))
    return SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        delete_message=AsyncMock(),
    )


def test_failed_llm_does_not_consume_quota_and_caption_is_reviewed(monkeypatch):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await _seed()
        bot = _moderation_settings(monkeypatch)
        model = AsyncMock(side_effect=[llm.LLMError("timeout"), {"label": "ok", "confidence": 1}])
        monkeypatch.setattr(moderation, "moderate_message", model)
        await moderation.on_group_text(_text_message("hello", caption=True), bot)
        async with session_scope() as session:
            assert (await session.get(UserChatState, (-100123, 55))).llm_messages_seen == 0
        await moderation.on_group_text(_text_message("hello again", mid=43), bot)
        async with session_scope() as session:
            assert (await session.get(UserChatState, (-100123, 55))).llm_messages_seen == 1
        assert model.call_args_list[0].kwargs["text"] == "hello"
        await engine.dispose()
    asyncio.run(run())


def test_parallel_messages_obey_successful_review_limit(monkeypatch):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await _seed()
        async with session_scope() as session:
            (await session.get(GroupSettings, -100123)).llm_max_messages = 3
        bot = _moderation_settings(monkeypatch)
        model = AsyncMock(return_value={"label": "ok", "confidence": 1})
        monkeypatch.setattr(moderation, "moderate_message", model)
        await asyncio.gather(*[moderation.on_group_text(_text_message(mid=i), bot) for i in range(10)])
        assert model.await_count == 3
        async with session_scope() as session:
            assert (await session.get(UserChatState, (-100123, 55))).llm_messages_seen == 3
        await engine.dispose()
    asyncio.run(run())


def test_unknown_edit_is_skipped_after_initial_message_quota(monkeypatch):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await _seed()
        async with session_scope() as session:
            session.add(UserChatState(chat_id=-100123, user_id=55, verified=True, llm_messages_seen=5))
        bot = _moderation_settings(monkeypatch)
        model = AsyncMock(return_value={"label": "spam", "confidence": 1})
        monkeypatch.setattr(moderation, "moderate_message", model)
        await moderation.router.edited_message.trigger(_text_message("spam"), bot=bot)
        model.assert_not_awaited()
        bot.delete_message.assert_not_awaited()
        async with session_scope() as session:
            assert (await session.get(UserChatState, (-100123, 55))).llm_messages_seen == 5
        await engine.dispose()
    asyncio.run(run())


def test_slow_old_verdict_cannot_delete_corrected_edit(monkeypatch):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await _seed()
        bot = _moderation_settings(monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()

        async def model(**kw):
            if kw["text"] == "spam":
                entered.set()
                await release.wait()
                return {"label": "spam", "confidence": 1}
            return {"label": "ok", "confidence": 1}

        monkeypatch.setattr(moderation, "moderate_message", model)
        original = asyncio.create_task(moderation.on_group_text(_text_message("spam"), bot))
        await entered.wait()
        edited = asyncio.create_task(moderation.on_edited_group_text(_text_message("corrected"), bot))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(original, edited)
        bot.delete_message.assert_not_called()
        await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("release_fails", [False, True])
def test_invite_failure_keeps_recoverable_state_until_unmute(monkeypatch, release_fails):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await _seed()
        monkeypatch.setattr(verify_flow, "get_settings", lambda: SimpleNamespace(global_admin_id_set=set()))
        monkeypatch.setattr(verify_flow, "_send_verify_invite_message", AsyncMock(return_value=None))
        schedule = Mock()
        monkeypatch.setattr(verify_flow, "schedule_verification_timeout", schedule)
        error = TelegramNetworkError(method=RestrictChatMember(chat_id=-100123, user_id=55, permissions={}), message="offline")
        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(side_effect=[True, error if release_fails else True]),
            get_chat_member=AsyncMock(side_effect=[
                SimpleNamespace(status="member"), SimpleNamespace(status="restricted", is_member=True),
            ]),
        )
        await verify_flow._start_verification_for_member(bot, chat_id=-100123, user_id=55, chat_title="Test")
        schedule.assert_called_once()
        async with session_scope() as session:
            token = (await session.scalars(select(VerifyToken))).one()
            state = await session.get(UserChatState, (-100123, 55))
            assert (token.used_at is None) is release_fails
            assert state.verified is not release_fails
        await engine.dispose()
    asyncio.run(run())


def test_invite_network_error_returns_failure(monkeypatch):
    monkeypatch.setattr(verify_flow, "_cached_bot_username", AsyncMock(return_value="testbot"))
    error = TelegramNetworkError(method=SendMessage(chat_id=-100123, text="verify"), message="offline")
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=error))
    assert asyncio.run(verify_flow._send_verify_invite_message(
        bot, target_chat_id=-100123, text="verify", settings=SimpleNamespace(), token="token",
        loc="en", message_thread_id=None, channel_label="group",
    )) is None


def test_626_stale_tokens_restore_without_waiting_for_telegram(monkeypatch):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await _seed()
        async with session_scope() as session:
            session.add_all([
                VerifyToken(token=f"stale-{uid}", chat_id=-100123, user_id=uid, expires_at=utc_now()-timedelta(minutes=5))
                for uid in range(626)
            ])
        entered = []
        blocked = asyncio.Event()

        async def expiry(*args, **kw):
            entered.append(kw["user_id"])
            await blocked.wait()

        monkeypatch.setattr(verify_flow, "_expire_verification", expiry)
        monkeypatch.setattr(verify_flow, "_timeout_semaphore", asyncio.Semaphore(4))
        try:
            assert await asyncio.wait_for(verify_flow.restore_verification_timeouts(SimpleNamespace()), timeout=2) == 626
            await asyncio.sleep(0.02)
            assert len(entered) == 4
            assert len(verify_flow._verification_tasks) == 626
        finally:
            await verify_flow.stop_verification_timeouts()
            await engine.dispose()
    asyncio.run(run())


def test_timeout_retries_transient_telegram_failure(monkeypatch):
    error = TelegramNetworkError(method=SendMessage(chat_id=-100123, text="unused"), message="offline")
    monkeypatch.setattr(verify_flow, "_expire_verification", AsyncMock(side_effect=error))
    schedule = Mock()
    monkeypatch.setattr(verify_flow, "schedule_verification_timeout", schedule)
    asyncio.run(verify_flow._verify_timeout_task(
        SimpleNamespace(), chat_id=-100123, user_id=55, token="token", delay=0, kick=True,
    ))
    assert schedule.call_args.kwargs["delay"] == 60
    assert schedule.call_args.kwargs["token"] == "token"
