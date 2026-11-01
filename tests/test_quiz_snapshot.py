from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from anticheatbot.db.models import GroupSettings, QuizQuestion, VerifyQuizSession, VerifyToken
from anticheatbot.db.session import configure_engine, session_scope
from anticheatbot.services.quiz import grade_answers
from anticheatbot.web import verify_api
from tests.test_verify_api_transactions import CHAT, TOKEN, _request, _seed


@pytest.mark.parametrize("mutation", ["edit", "delete"])
def test_question_changes_preserve_display_grading_and_threshold(tmp_path, mutation) -> None:
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/quiz.sqlite")
        try:
            await _seed("quiz")
            async with session_scope() as session:
                question = await session.get(QuizQuestion, 1)
                if mutation == "delete":
                    await session.delete(question)
                else:
                    question.prompt = "Changed prompt"
                    question.choices_json = ["B", "A"]
                    question.correct_index = 1
                (await session.get(GroupSettings, CHAT)).quiz_pass_min_correct = 10
            async with session_scope() as session:
                assert await grade_answers(session, token=TOKEN, answers={1: 0}, quiz_version="test-version") == (1, 1, 1)
            request = _request(AsyncMock())
            request.rel_url = SimpleNamespace(query={"t": TOKEN})
            response = await verify_api.verify_session(request)
            payload = json.loads(response.text)
            assert payload["quiz"] == [{"id": 1, "prompt": "Choose A", "choices": ["A", "B"]}]
            assert payload["quiz_version"] == "test-version"
            assert payload["quiz_pass_min_correct"] == 1
            assert "correct_index" not in response.text
            assert "__snapshot" not in response.text
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_legacy_or_stale_browser_cannot_be_graded_against_new_version(tmp_path) -> None:
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path}/quiz.sqlite")
        try:
            await _seed("quiz")
            async with session_scope() as session:
                (await session.get(VerifyQuizSession, TOKEN)).shuffle_maps_json = {"1": [0, 1]}
            bot = AsyncMock()
            request = _request(bot, answers={"1": 0})
            with pytest.raises(web.HTTPConflict) as exc:
                await verify_api.verify_quiz_submit(request)
            error_payload = json.loads(exc.value.text)
            assert error_payload["error"] == "quiz_changed"
            assert "reopen the verification link" in error_payload["message"]
            bot.ban_chat_member.assert_not_awaited()
            bot.restrict_chat_member.assert_not_awaited()
            async with session_scope() as session:
                assert (await session.get(VerifyToken, TOKEN)).used_at is None
            request.rel_url = SimpleNamespace(query={"t": TOKEN})
            refreshed = json.loads((await verify_api.verify_session(request)).text)
            assert refreshed["quiz_version"] != "test-version"
            with pytest.raises(web.HTTPConflict):
                await verify_api.verify_quiz_submit(request)
        finally:
            await engine.dispose()
    asyncio.run(run())
