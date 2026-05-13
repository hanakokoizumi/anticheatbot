from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from anticheatbot.db.models import AuditEvent


async def log_event(
    session: AsyncSession,
    *,
    event_type: str,
    chat_id: int | None = None,
    user_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            event_type=event_type,
            chat_id=chat_id,
            user_id=user_id,
            payload_json=payload,
        )
    )
    await session.flush()
