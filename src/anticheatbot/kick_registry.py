"""Track bot-initiated kicks so we can delete the Telegram service ``left_chat_member`` message."""

from __future__ import annotations

import asyncio
import time

_lock = asyncio.Lock()
_entries: list[tuple[int, int, float]] = []

_TTL_SEC = 90.0


def _prune(now: float) -> None:
    global _entries
    _entries = [e for e in _entries if now - e[2] < _TTL_SEC]


async def register_recent_bot_kick(*, chat_id: int, user_id: int) -> None:
    """Call immediately before ``ban_chat_member`` / kick so the next service message can be matched."""
    async with _lock:
        now = time.monotonic()
        _prune(now)
        _entries.append((chat_id, user_id, now))


async def pop_matching_recent_bot_kick(*, chat_id: int, kicked_user_id: int) -> bool:
    """If this ``left_chat_member`` matches a recent bot kick, remove the registry entry and return True."""
    async with _lock:
        now = time.monotonic()
        _prune(now)
        for i, (cid, uid, _ts) in enumerate(_entries):
            if cid == chat_id and uid == kicked_user_id:
                _entries.pop(i)
                return True
        return False
