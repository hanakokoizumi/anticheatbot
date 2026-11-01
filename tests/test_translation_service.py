from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from anticheatbot import translation_service
from anticheatbot import llm
from anticheatbot.db.models import LLMTranslationCache
from anticheatbot.db.session import configure_engine, create_all, session_scope
from anticheatbot.translation_service import _allowed_target, sha256_utf8, translate_or_same


def test_sha256_utf8_stable() -> None:
    assert sha256_utf8("hello") == sha256_utf8("hello")
    assert len(sha256_utf8("x")) == 64


@pytest.mark.parametrize(
    ("raw", "target", "expected"),
    [
        (None, "en", True),
        ("", "en", True),
        ('["en","ja"]', "en", True),
        ('["en","ja"]', "ko", False),
        ("not-json", "en", False),
        ("null", "en", False),
        ("42", "en", False),
        ('{"en": true}', "en", False),
        ('[["en"]]', "en", False),
        ('["en", false]', "en", False),
    ],
)
def test_allowed_target(raw: str | None, target: str, expected: bool) -> None:
    gs = SimpleNamespace(translation_allowed_locales=raw)
    assert _allowed_target(gs, target) is expected


def _settings(model="gpt-6-luna"):
    return SimpleNamespace(
        openai_api_key="test", openai_base_url="https://example.test", translation_prompt_version=1,
        effective_translate_model=model,
    )


def _group():
    return SimpleNamespace(canonical_locale="en", llm_translation_enabled=True, translation_allowed_locales=None)


async def _translate(session, settings):
    return await translate_or_same(
        session, settings, _group(), namespace="rules", entity_id="rules", chat_id=-100123,
        text="Group rules", target_locale="ja",
    )


def test_switching_model_refreshes_cache_without_new_schema(monkeypatch):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await create_all()
        translator = AsyncMock(side_effect=["旧模型翻译", "Luna 翻译"])
        monkeypatch.setattr(translation_service, "translate_markdown", translator)
        async with session_scope() as session:
            assert await _translate(session, _settings("old-model")) == ("旧模型翻译", False)
            # Keep an old ORM copy in the identity map while refreshing in the same transaction.
            cached = await session.scalar(select(LLMTranslationCache))
            assert cached.model == "old-model"
            assert await _translate(session, _settings()) == ("Luna 翻译", False)
            assert await _translate(session, _settings()) == ("Luna 翻译", True)
        async with session_scope() as session:
            assert await session.scalar(select(func.count()).select_from(LLMTranslationCache)) == 1
            stored = await session.scalar(select(LLMTranslationCache))
            assert (stored.model, stored.translated_text) == ("gpt-6-luna", "Luna 翻译")
        assert translator.await_count == 2
        await engine.dispose()
    asyncio.run(run())


def test_concurrent_cache_misses_do_not_abort_transactions(monkeypatch, tmp_path):
    async def run():
        engine = configure_engine(f"sqlite+aiosqlite:///{tmp_path / 'translations.db'}")
        await create_all()
        calls = 0
        both_called = asyncio.Event()

        async def translate(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                both_called.set()
            await both_called.wait()
            return "同じ翻訳"

        monkeypatch.setattr(translation_service, "translate_markdown", translate)

        async def request():
            async with session_scope() as session:
                return await _translate(session, _settings())

        results = await asyncio.wait_for(asyncio.gather(request(), request()), timeout=5)
        assert results == [("同じ翻訳", False), ("同じ翻訳", False)]
        async with session_scope() as session:
            assert await session.scalar(select(func.count()).select_from(LLMTranslationCache)) == 1
            assert await _translate(session, _settings()) == ("同じ翻訳", True)
        assert calls == 2
        await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("blank", ["", " \n\t", "\u200b\u200d\ufeff", "\x00\x1b", "\ufe0f\u034f", "\u2800\u3164"])
def test_model_blank_or_invisible_translation_falls_back_without_cache(monkeypatch, blank):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await create_all()
        monkeypatch.setattr(llm, "chat_completion_json", AsyncMock(return_value={"translated": blank}))
        async with session_scope() as session:
            assert await _translate(session, _settings()) == ("Group rules", True)
            assert await session.scalar(select(func.count()).select_from(LLMTranslationCache)) == 0
        await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_legacy_blank_cache_is_refreshed_or_falls_back_to_source(monkeypatch, refresh_fails):
    async def run():
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await create_all()
        async with session_scope() as session:
            session.add(LLMTranslationCache(
                namespace="rules", entity_id="rules", chat_id=-100123, source_locale="en", target_locale="ja",
                source_sha256=sha256_utf8("Group rules"), prompt_version=1, translated_text="\u200b ", model="gpt-6-luna",
            ))
        translated = "\n  **グループのルール**\n"
        response = AsyncMock(side_effect=llm.LLMError("offline")) if refresh_fails else AsyncMock(return_value={"translated": translated})
        monkeypatch.setattr(llm, "chat_completion_json", response)
        async with session_scope() as session:
            result = await _translate(session, _settings())
            assert result == (("Group rules", True) if refresh_fails else (translated, False))
            response.assert_awaited_once()
            assert await session.scalar(select(func.count()).select_from(LLMTranslationCache)) == 1
        if not refresh_fails:
            async with session_scope() as session:
                assert await _translate(session, _settings()) == (translated, True)
                response.assert_awaited_once()
        await engine.dispose()
    asyncio.run(run())
