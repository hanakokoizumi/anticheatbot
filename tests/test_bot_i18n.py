from __future__ import annotations

import pytest

from anticheatbot.bot_i18n import MESSAGES, bot_t, resolve_bot_locale


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (None, "zh-Hans"),
        ("", "zh-Hans"),
        ("  ", "zh-Hans"),
        ("zh", "zh-Hans"),
        ("zh_CN", "zh-Hans"),
        ("zh-TW", "zh-Hans"),
        ("en", "en"),
        ("en-US", "en"),
        ("ja", "ja"),
        ("ja-JP", "ja"),
        ("ko", "ko"),
        ("ko-KR", "ko"),
        ("de", "en"),
    ],
)
def test_resolve_bot_locale(code: str | None, expected: str) -> None:
    assert resolve_bot_locale(code) == expected


def test_bot_t_known_keys_all_locales() -> None:
    for loc in ("zh-Hans", "en", "ja", "ko"):
        assert bot_t(loc, "btn_open_verify") == MESSAGES[loc]["btn_open_verify"]


def test_bot_t_fallback_en_for_partial_locale() -> None:
    """If a locale lacked a key, bot_t falls back to en then zh-Hans — all keys present in all locales."""
    assert len(bot_t("ko", "cmd_start")) > 20
