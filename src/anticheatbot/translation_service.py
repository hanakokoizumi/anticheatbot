from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from anticheatbot.config import Settings
from anticheatbot.db.models import GroupSettings, LLMTranslationCache
from anticheatbot.llm import LLMError, has_visible_text, translate_markdown

log = logging.getLogger(__name__)


def sha256_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _allowed_target(gs: GroupSettings, target: str) -> bool:
    raw = (gs.translation_allowed_locales or "").strip()
    if not raw:
        return True
    try:
        allowed = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        return False
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
    model = settings.effective_translate_model
    cache_key = dict(
        namespace=namespace, chat_id=chat_id, entity_id=entity_id,
        source_locale=src, target_locale=target_locale, source_sha256=digest, prompt_version=pv,
    )
    # Read columns rather than ORM instances: a prior read in this transaction must
    # not keep an identity-map copy of a row refreshed through the upsert below.
    stmt = select(LLMTranslationCache.translated_text, LLMTranslationCache.model).where(
        *(getattr(LLMTranslationCache, key) == value for key, value in cache_key.items())
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is not None and row.model == model and has_visible_text(row.translated_text):
        return row.translated_text, True

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

    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    cache = insert(LLMTranslationCache).values(
        **cache_key, translated_text=translated, model=model,
    ).on_conflict_do_update(
        index_elements=list(cache_key),
        set_={"translated_text": translated, "model": model, "created_at": func.now()},
    )
    await session.execute(cache)
    return translated, False
