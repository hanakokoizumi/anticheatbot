from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from anticheatbot.db.models import GroupSettings, KnownChat


async def get_or_create_group_settings(session: AsyncSession, chat_id: int) -> GroupSettings:
    row = await session.get(GroupSettings, chat_id)
    if row is not None:
        return row
    row = GroupSettings(chat_id=chat_id)
    session.add(row)
    await session.flush()
    return row


async def touch_known_chat(session: AsyncSession, chat_id: int, title: str | None) -> None:
    row = await session.get(KnownChat, chat_id)
    if row is None:
        session.add(KnownChat(chat_id=chat_id, title=title))
    else:
        row.title = title
    await session.flush()


async def list_known_chat_ids(session: AsyncSession) -> list[int]:
    stmt = select(KnownChat.chat_id)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)
