from __future__ import annotations

import hashlib
import json
import logging
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from anticheatbot.config import Settings
from anticheatbot.db.models import GroupSettings, LLMTranslationCache
from anticheatbot.llm import LLMError, translate_markdown

log = logging.getLogger(__name__)


def sha256_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _allowed_target(gs: GroupSettings, target: str) -> bool:
    raw = (gs.translation_allowed_locales or "").strip()
    if not raw:
        return True
    try:
        allowed: Iterable[str] = json.loads(raw)
    except json.JSONDecodeError:
        return True
    return target in set(allowed)


async def translate_or_same(
    session: AsyncSession,
    settings: Settings,
    gs: GroupSettings,
    *,
    namespace: str,
    entity_id: str,
    chat_id: int,
    text: str,
    target_locale: str,
) -> tuple[str, bool]:
    """Return (text, cache_hit)."""
    if not text.strip():
        return text, True
    src = gs.canonical_locale
    if target_locale.lower() == src.lower():
        return text, True
    if not gs.llm_translation_enabled or not settings.openai_api_key:
        return text, True
    if not _allowed_target(gs, target_locale):
        return text, True

    digest = sha256_utf8(text)
    pv = settings.translation_prompt_version
    stmt = select(LLMTranslationCache).where(
        LLMTranslationCache.namespace == namespace,
        LLMTranslationCache.chat_id == chat_id,
        LLMTranslationCache.entity_id == entity_id,
        LLMTranslationCache.source_locale == src,
        LLMTranslationCache.target_locale == target_locale,
        LLMTranslationCache.source_sha256 == digest,
        LLMTranslationCache.prompt_version == pv,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row:
        return row.translated_text, True

    model = settings.effective_translate_model
    try:
        translated = await translate_markdown(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            model=model,
            text=text,
            source_locale=src,
            target_locale=target_locale,
        )
    except (LLMError, OSError) as e:
        log.warning("translation failed, falling back to source: %s", e)
        return text, True

    cache = LLMTranslationCache(
        namespace=namespace,
        chat_id=chat_id,
        entity_id=entity_id,
        source_locale=src,
        target_locale=target_locale,
        source_sha256=digest,
        prompt_version=pv,
        translated_text=translated,
        model=model,
    )
    session.add(cache)
    await session.flush()
    return translated, False
