"""Malformed model numbers must fail safely without charging review quota."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from anticheatbot import llm
from anticheatbot.db.models import UserChatState
from anticheatbot.db.session import configure_engine, session_scope
from anticheatbot.handlers import moderation
from tests.test_bot_audit_regressions import _moderation_settings, _seed, _text_message


@pytest.mark.parametrize("confidence", [
    pytest.param(10 ** 400, id="huge-positive-integer"),
    pytest.param(-(10 ** 400), id="huge-negative-integer"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="infinity"),
    pytest.param(-float("inf"), id="negative-infinity"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
])
def test_bad_numeric_verdict_is_controlled_and_does_not_consume_quota(monkeypatch, confidence):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _seed()
            bot = _moderation_settings(monkeypatch)
            response = AsyncMock(side_effect=[
                {"label": "spam", "confidence": confidence},
                {"label": "ok", "confidence": 1},
            ])
            monkeypatch.setattr(llm, "chat_completion_json", response)
            # Keep the actual model verdict validator and full handler quota path.
            await moderation.on_group_text(_text_message("first"), bot)
            bot.delete_message.assert_not_awaited()
            async with session_scope() as session:
                assert (await session.get(UserChatState, (-100123, 55))).llm_messages_seen == 0
            await moderation.on_group_text(_text_message("second", mid=43), bot)
            async with session_scope() as session:
                assert (await session.get(UserChatState, (-100123, 55))).llm_messages_seen == 1
            assert response.await_count == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("confidence", [0, 1])
def test_valid_confidence_boundaries_are_preserved(monkeypatch, confidence):
    verdict = {"label": "ok", "confidence": confidence}
    monkeypatch.setattr(llm, "chat_completion_json", AsyncMock(return_value=verdict))
    assert asyncio.run(llm.moderate_message(
        base_url="https://example.test", api_key="test", model="gpt-6-luna", text="hello",
    )) == verdict
