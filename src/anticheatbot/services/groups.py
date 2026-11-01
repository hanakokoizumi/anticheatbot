from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from anticheatbot.db.models import GroupSettings, KnownChat

_guest_bot_blocked_chats: set[int] = set()
_pending_member_mutes: dict[tuple[int, int], int] = {}
_verification_exempt_members: set[tuple[int, int]] = set()


def member_mute_pending(chat_id: int, user_id: int) -> bool:
    return (chat_id, user_id) in _pending_member_mutes


def set_member_mute_pending(chat_id: int, user_id: int, pending: bool) -> None:
    key = (chat_id, user_id)
    if pending:
        _pending_member_mutes[key] = _pending_member_mutes.get(key, 0) + 1
    else:
        remaining = _pending_member_mutes.get(key, 0) - 1
        if remaining > 0:
            _pending_member_mutes[key] = remaining
        else:
            _pending_member_mutes.pop(key, None)


def member_verification_exempt(chat_id: int, user_id: int) -> bool:
    return (chat_id, user_id) in _verification_exempt_members


def set_member_verification_exempt(chat_id: int, user_id: int, exempt: bool) -> None:
    key = (chat_id, user_id)
    if exempt:
        _verification_exempt_members.add(key)
    else:
        _verification_exempt_members.discard(key)


def guest_bot_block_enabled(chat_id: int) -> bool:
    return chat_id in _guest_bot_blocked_chats


def set_guest_bot_block_enabled(chat_id: int, enabled: bool) -> None:
    if enabled:
        _guest_bot_blocked_chats.add(chat_id)
    else:
        _guest_bot_blocked_chats.discard(chat_id)


async def load_guest_bot_blocked_chats(session: AsyncSession) -> None:
    rows = await session.scalars(
        select(GroupSettings.chat_id).where(GroupSettings.block_guest_bots.is_(True))
    )
    _guest_bot_blocked_chats.clear()
    _guest_bot_blocked_chats.update(rows.all())


async def get_or_create_group_settings(session: AsyncSession, chat_id: int) -> GroupSettings:
    row = await session.get(GroupSettings, chat_id)
    if row is not None:
        return row
    # Different members can deliver their first updates concurrently for a new group.
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    await session.execute(insert(GroupSettings).values(chat_id=chat_id).on_conflict_do_nothing(index_elements=["chat_id"]))
    return await session.get(GroupSettings, chat_id)


async def touch_known_chat(session: AsyncSession, chat_id: int, title: str | None) -> None:
    row = await session.get(KnownChat, chat_id)
    if row is not None:
        # Ordinary member updates must not acquire a shared group-row write lock.
        if title is not None and title != row.title:
            row.title = title
            await session.flush()
        return
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    stmt = insert(KnownChat).values(chat_id=chat_id, title=title)
    if title is None:
        stmt = stmt.on_conflict_do_nothing(index_elements=["chat_id"])
    else:
        stmt = stmt.on_conflict_do_update(index_elements=["chat_id"], set_={"title": title})
    await session.execute(stmt)


async def list_known_chat_ids(session: AsyncSession) -> list[int]:
    stmt = select(KnownChat.chat_id)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)
