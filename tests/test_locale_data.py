from __future__ import annotations

import pytest

from anticheatbot.locale_data import normalize_locale


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (None, "en"),
        ("", "en"),
        ("zh", "zh-Hans"),
        ("zh-CN", "zh-Hans"),
        ("zh_SG", "zh-Hans"),
        ("zh-TW", "zh-Hant"),
        ("ZH_hk", "zh-Hant"),
        ("en", "en"),
        ("EN", "en"),
        ("ja", "ja"),
        ("ja-JP", "ja-JP"),
    ],
)
def test_normalize_locale(code: str | None, expected: str) -> None:
    assert normalize_locale(code) == expected
