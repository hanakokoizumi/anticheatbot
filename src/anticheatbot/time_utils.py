"""Datetime helpers: SQLite often returns naive datetimes even for timezone=True columns."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def coerce_utc(dt: datetime) -> datetime:
    """Make a datetime UTC-aware for safe comparisons (ORM may return naive values from SQLite)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
