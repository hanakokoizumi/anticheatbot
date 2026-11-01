from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import DeleteMessage
from sqlalchemy import func, select

from anticheatbot.db.models import QuizQuestion, VerifyToken
from anticheatbot.db.session import configure_engine, session_scope
from anticheatbot.time_utils import utc_now
from anticheatbot.web import admin_api, verify_api
from anticheatbot.web.json_body import json_object
from tests.test_verify_api_transactions import CHAT, TOKEN, _request, _seed


@pytest.mark.parametrize("bad", ['null', '42', 'true', '{}', '[["en"]]', '[null]', '[""]', '{"en":true}', 'not json'])
def test_translation_locale_shape_rejected_at_save(bad):
    with pytest.raises(web.HTTPBadRequest):
        admin_api._coerce_admin_settings_value("translation_allowed_locales", bad)


def test_translation_locales_are_normalized_and_deduplicated():
    value = admin_api._coerce_admin_settings_value("translation_allowed_locales", '["EN", "en", "zh_CN"]')
    assert json.loads(value) == ["en", "zh-Hans"]
    assert admin_api._coerce_admin_settings_value("translation_allowed_locales", " ") is None
    assert admin_api._coerce_admin_settings_value("translation_allowed_locales", "[]") == "[]"


def test_oversized_confidence_and_database_ids_are_client_errors():
    with pytest.raises(web.HTTPBadRequest):
        admin_api._coerce_admin_settings_value("llm_min_confidence_action", 10 ** 500)
    with pytest.raises(web.HTTPBadRequest):
        admin_api._chat_id_param(SimpleNamespace(rel_url=SimpleNamespace(query={"chat_id": str(10 ** 100)})))


def test_locale_limits_match_database_column_width():
    with pytest.raises(web.HTTPBadRequest):
        admin_api._coerce_admin_settings_value("canonical_locale", "x" * 33)
    request = SimpleNamespace(rel_url=SimpleNamespace(query={"lang": "x" * 64}))
    assert len(verify_api._target_locale(request, {})) == 32


@pytest.mark.parametrize("qid", [1.5, True, False, 2 ** 40])
def test_invalid_question_id_cannot_edit_another_question(tmp_path, monkeypatch, qid):
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/question-id.sqlite")
        try:
            await _seed("quiz")
            request = _request(AsyncMock())
            request.rel_url = SimpleNamespace(query={"chat_id": str(CHAT)})
            request.json = AsyncMock(return_value={"id": qid, "prompt": "Changed", "choices": ["x", "y"], "correct_index": 0})
            monkeypatch.setattr(admin_api, "_require_admin_access", AsyncMock())
            with pytest.raises(web.HTTPBadRequest):
                await admin_api.admin_quiz_upsert(request)
            async with session_scope() as session:
                assert await session.scalar(select(func.count()).select_from(QuizQuestion)) == 1
                assert (await session.get(QuizQuestion, 1)).prompt == "Choose A"
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("error", [UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"), ValueError("integer too long")])
def test_invalid_json_encoding_is_a_client_error(error):
    request = MagicMock(content_length=None)
    request.json = AsyncMock(side_effect=error)
    with pytest.raises(web.HTTPBadRequest):
        asyncio.run(json_object(request))


def test_slow_translation_returns_source_quiz_and_can_still_be_submitted(tmp_path, monkeypatch):
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/slow-translation.sqlite")
        try:
            await _seed("quiz")
            was_cancelled = False

            async def translate(*args, **kwargs):
                nonlocal was_cancelled
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    was_cancelled = True
                    raise
                return "unreachable", False

            monkeypatch.setattr(verify_api, "translate_or_same", translate)
            monkeypatch.setattr(verify_api, "_TRANSLATION_BUDGET_SECONDS", 0.01)
            bot = AsyncMock()
            bot.get_chat_member.return_value = SimpleNamespace(status="restricted", is_member=True)
            request = _request(bot)
            request.rel_url = SimpleNamespace(query={"t": TOKEN})
            response = await asyncio.wait_for(verify_api.verify_session(request), timeout=1)
            payload = json.loads(response.text)
            assert was_cancelled
            assert payload["translation_fallback"] == "timeout"
            assert payload["quiz"] == [{"id": 1, "prompt": "Choose A", "choices": ["A", "B"]}]
            assert payload["quiz_version"] == "test-version"
            assert (await verify_api.verify_quiz_submit(_request(bot, answers={"1": 0}))).status == 200
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_token_expiring_during_translation_does_not_return_live_quiz(tmp_path, monkeypatch):
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/expired-translation.sqlite")
        try:
            await _seed()

            async def translate(*args, **kwargs):
                async with session_scope() as session:
                    (await session.get(VerifyToken, TOKEN)).expires_at = utc_now() - timedelta(seconds=1)
                return kwargs["text"], True

            monkeypatch.setattr(verify_api, "translate_or_same", translate)
            request = _request(AsyncMock())
            request.rel_url = SimpleNamespace(query={"t": TOKEN})
            with pytest.raises(web.HTTPForbidden, match="Forbidden") as exc:
                await verify_api.verify_session(request)
            assert exc.value.text == "token expired"
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["rules_ack", "quiz"])
def test_successful_submission_survives_invite_cleanup_network_failure(tmp_path, mode):
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/cleanup.sqlite")
        try:
            await _seed(mode)
            async with session_scope() as session:
                token = await session.get(VerifyToken, TOKEN)
                token.invite_message_chat_id = CHAT
                token.invite_message_id = 42
            bot = AsyncMock()
            bot.get_chat_member.return_value = SimpleNamespace(status="restricted", is_member=True)
            bot.delete_message.side_effect = TelegramNetworkError(method=DeleteMessage(chat_id=CHAT, message_id=42), message="timeout")
            request = _request(bot, answers={"1": 0} if mode == "quiz" else None)
            handler = verify_api.verify_quiz_submit if mode == "quiz" else verify_api.verify_rules_complete
            assert (await handler(request)).status == 200
            async with session_scope() as session:
                assert (await session.get(VerifyToken, TOKEN)).used_at is not None
        finally:
            await engine.dispose()
    asyncio.run(run())
