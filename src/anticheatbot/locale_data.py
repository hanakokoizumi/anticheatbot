"""Locale normalization for Telegram language_code."""

from __future__ import annotations


def normalize_locale(code: str | None) -> str:
    if not code:
        return "en"
    c = code.strip().replace("_", "-")
    low = c.lower()
    if low in {"zh-cn", "zh-hans", "zh-sg"}:
        return "zh-Hans"
    if low in {"zh-tw", "zh-hant", "zh-hk", "zh-mo"}:
        return "zh-Hant"
    if low == "zh":
        return "zh-Hans"
    if len(low) == 2:
        return low
    return c
