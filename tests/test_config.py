from __future__ import annotations

import pytest
from pydantic import ValidationError

from anticheatbot.config import Settings


def test_webapp_public_url_strips_trailing_slash() -> None:
    s = Settings(BOT_TOKEN="1:a", WEBAPP_PUBLIC_URL="https://example.com/")
    assert s.webapp_public_url == "https://example.com"


def test_webapp_public_url_requires_https() -> None:
    with pytest.raises(ValidationError):
        Settings(BOT_TOKEN="1:a", WEBAPP_PUBLIC_URL="http://example.com")


def test_global_admin_id_set_merges_and_skips_invalid() -> None:
    s = Settings(
        BOT_TOKEN="1:a",
        WEBAPP_PUBLIC_URL="https://x",
        GLOBAL_ADMIN_USER_IDS="1, 2, bad",
        ADMIN_USER_IDS="3, 2",
    )
    assert s.global_admin_id_set == {1, 2, 3}


def test_effective_models_fallback() -> None:
    s = Settings(BOT_TOKEN="1:a", WEBAPP_PUBLIC_URL="https://x", OPENAI_MODEL="gpt-test")
    assert s.effective_translate_model == "gpt-test"
    assert s.effective_moderation_model == "gpt-test"
    s2 = Settings(
        BOT_TOKEN="1:a",
        WEBAPP_PUBLIC_URL="https://x",
        OPENAI_MODEL="base",
        TRANSLATE_MODEL="tr",
        MODERATION_MODEL="mod",
    )
    assert s2.effective_translate_model == "tr"
    assert s2.effective_moderation_model == "mod"
