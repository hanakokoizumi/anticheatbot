"""Bounded retries for transient Telegram failures; the first request is always immediate."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError

T = TypeVar("T")


async def retry_telegram(
    operation: Callable[[], Awaitable[T]], *, initial_error: TelegramAPIError | None = None,
    max_attempts: int = 3, max_wait_seconds: float = 60.0,
) -> T:
    """Retry in the caller's task, never spawn one task per retry or ignore a server cooldown."""
    attempts = 1 if initial_error is not None else 0
    error = initial_error
    waited = 0.0
    while attempts < max_attempts:
        if error is not None:
            if isinstance(error, TelegramRetryAfter):
                delay = max(0.0, float(error.retry_after)) + 0.1
            elif isinstance(error, (TelegramNetworkError, TelegramServerError)):
                delay = min(2.0 ** (attempts - 1), 5.0)
            else:
                raise error
            if waited + delay > max_wait_seconds:
                raise error
            await asyncio.sleep(delay)
            waited += delay
        attempts += 1
        try:
            return await operation()
        except TelegramAPIError as exc:
            error = exc
    assert error is not None
    raise error
