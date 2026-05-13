from __future__ import annotations

from types import SimpleNamespace

import pytest

from anticheatbot.translation_service import _allowed_target, sha256_utf8


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
        ("not-json", "en", True),
    ],
)
def test_allowed_target(raw: str | None, target: str, expected: bool) -> None:
    gs = SimpleNamespace(translation_allowed_locales=raw)
    assert _allowed_target(gs, target) is expected
