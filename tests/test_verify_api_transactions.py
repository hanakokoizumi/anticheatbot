from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.enums import ChatMemberStatus
from aiogram.methods import RestrictChatMember, UnbanChatMember

from anticheatbot.config import Settings
from anticheatbot.db.models import GroupSettings, KnownChat, QuizQuestion, UserChatState, VerifyQuizSession, VerifyToken
from anticheatbot.db.session import configure_engine, create_all, session_scope
from anticheatbot.telegram_perms import normal_member_permissions
from anticheatbot.time_utils import utc_now
from anticheatbot.web import verify_api
from tests.signing import sign_webapp_init_data

TOKEN = "verification_test_token_12345678"
CHAT = -100987654321
USER = 1234


async def _seed(mode: str = "rules_ack") -> None:
    await create_all()
    async with session_scope() as session:
        session.add(KnownChat(chat_id=CHAT))
        await session.flush()
        session.add(GroupSettings(chat_id=CHAT, verify_mode=mode, quiz_draw_count=1, quiz_pass_min_correct=1))
        session.add(VerifyToken(token=TOKEN, chat_id=CHAT, user_id=USER, expires_at=utc_now() + timedelta(minutes=5)))
        session.add(UserChatState(chat_id=CHAT, user_id=USER, verified=False))
        await session.flush()
        if mode == "quiz":
            session.add(QuizQuestion(id=1, chat_id=CHAT, prompt="Choose A", choices_json=["A", "B"], correct_index=0, points=1))
            session.add(VerifyQuizSession(token=TOKEN, question_ids_json=[1], shuffle_maps_json={
                "1": [0, 1],
                "__snapshot_v1": {"version": "test-version", "required_correct": 1, "questions": [
                    {"id": 1, "prompt": "Choose A", "choices": ["A", "B"], "correct_index": 0}
                ]},
            }))


def _request(bot, *, answers=None):
    settings = Settings(BOT_TOKEN="12345:fake", WEBAPP_PUBLIC_URL="https://example.com")
    request = MagicMock()
    request.app = {"settings": settings, "bot": bot}
    request.headers = {"X-Telegram-Init-Data": sign_webapp_init_data(bot_token=settings.bot_token, user={"id": USER})}
    body = {"token": TOKEN}
    if answers is not None:
        body["answers"] = answers
        body["quiz_version"] = "test-version"
    request.content_length = None
    request.json = AsyncMock(return_value=body)
    request.remote = "127.0.0.1"
    return request


@pytest.mark.parametrize("mode", ["rules_ack", "quiz"])
@pytest.mark.parametrize("error_type", [TelegramBadRequest, TelegramForbiddenError])
def test_unrestrict_failure_rolls_back_and_allows_retry(tmp_path, mode, error_type) -> None:
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/verify.sqlite")
        try:
            await _seed(mode)
            method = RestrictChatMember(chat_id=CHAT, user_id=USER, permissions=normal_member_permissions())
            bot = AsyncMock()
            bot.get_chat_member.return_value = SimpleNamespace(status=ChatMemberStatus.RESTRICTED, is_member=True)
            bot.restrict_chat_member.side_effect = error_type(method=method, message="permissions unavailable")
            request = _request(bot, answers={"1": 0} if mode == "quiz" else None)
            handler = verify_api.verify_quiz_submit if mode == "quiz" else verify_api.verify_rules_complete
            with pytest.raises(web.HTTPServiceUnavailable):
                await handler(request)
            async with session_scope() as session:
                token = await session.get(VerifyToken, TOKEN)
                state = await session.get(UserChatState, (CHAT, USER))
                assert token.used_at is None
                assert state.verified is False
                if mode == "quiz":
                    assert await session.get(VerifyQuizSession, TOKEN) is not None
            bot.restrict_chat_member.side_effect = None
            assert (await handler(request)).status == 200
            async with session_scope() as session:
                assert (await session.get(VerifyToken, TOKEN)).used_at is not None
                assert (await session.get(UserChatState, (CHAT, USER))).verified is True
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_quiz_failure_does_not_kick_when_token_consumption_loses_race(tmp_path, monkeypatch) -> None:
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/verify.sqlite")
        try:
            await _seed("quiz")
            consume = AsyncMock(side_effect=web.HTTPForbidden(text="token expired or used"))
            monkeypatch.setattr(verify_api, "_consume_verify_token_once", consume)
            bot = AsyncMock()
            with pytest.raises(web.HTTPForbidden):
                await verify_api.verify_quiz_submit(_request(bot, answers={"1": 1}))
            consume.assert_awaited_once()
            bot.ban_chat_member.assert_not_awaited()
            bot.unban_chat_member.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_token_expired_during_challenge_cannot_restore_permissions(tmp_path, monkeypatch) -> None:
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/verify.sqlite")
        try:
            await _seed()
            now = utc_now()
            monkeypatch.setattr(verify_api, "utc_now", MagicMock(side_effect=[now, now + timedelta(minutes=10)]))
            bot = AsyncMock()
            with pytest.raises(web.HTTPForbidden):
                await verify_api.verify_rules_complete(_request(bot))
            bot.restrict_chat_member.assert_not_awaited()
            async with session_scope() as session:
                assert (await session.get(VerifyToken, TOKEN)).used_at is None
                assert (await session.get(UserChatState, (CHAT, USER))).verified is False
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("status", [ChatMemberStatus.KICKED, ChatMemberStatus.LEFT])
def test_stale_verification_cannot_restore_departed_member_permissions(tmp_path, status) -> None:
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/verify.sqlite")
        try:
            await _seed()
            bot = AsyncMock()
            bot.get_chat_member.return_value = SimpleNamespace(status=status)
            with pytest.raises(web.HTTPForbidden) as exc:
                await verify_api.verify_rules_complete(_request(bot))
            assert "membership_changed" in exc.value.text
            bot.restrict_chat_member.assert_not_awaited()
            async with session_scope() as session:
                assert (await session.get(VerifyToken, TOKEN)).used_at is None
                assert (await session.get(UserChatState, (CHAT, USER))).verified is False
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_quiz_failure_never_unbans_a_manually_banned_member(tmp_path) -> None:
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/verify.sqlite")
        try:
            await _seed("quiz")
            bot = AsyncMock()
            bot.get_chat_member.return_value = SimpleNamespace(status=ChatMemberStatus.KICKED)
            assert (await verify_api.verify_quiz_submit(_request(bot, answers={"1": 1}))).status == 403
            bot.ban_chat_member.assert_not_awaited()
            bot.unban_chat_member.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_quiz_failure_unban_network_error_leaves_finite_ban_and_consumed_token(tmp_path) -> None:
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/verify.sqlite")
        try:
            await _seed("quiz")
            bot = AsyncMock()
            bot.get_chat_member.return_value = SimpleNamespace(status=ChatMemberStatus.RESTRICTED, is_member=True)
            bot.unban_chat_member.side_effect = TelegramNetworkError(
                method=UnbanChatMember(chat_id=CHAT, user_id=USER), message="timeout"
            )
            started = utc_now()
            assert (await verify_api.verify_quiz_submit(_request(bot, answers={"1": 1}))).status == 403
            until_date = bot.ban_chat_member.call_args.kwargs["until_date"]
            assert 59 <= (until_date - started).total_seconds() <= 65
            assert bot.unban_chat_member.call_args.kwargs["only_if_banned"] is True
            async with session_scope() as session:
                assert (await session.get(VerifyToken, TOKEN)).used_at is not None
        finally:
            await engine.dispose()
    asyncio.run(run())
