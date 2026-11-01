"""Delete spam from Telegram Guest Bots (Bot API guest_query_id / guest_message updates)."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from anticheatbot.config import get_settings
from anticheatbot.handlers.verify_flow import pending_join_needs_verification
from anticheatbot.db.models import GroupSettings, VerifyToken
from anticheatbot.db.session import session_scope
from anticheatbot.services.audit import log_event
from anticheatbot.services.groups import (
    get_or_create_group_settings,
    guest_bot_block_enabled,
    member_mute_pending,
    member_verification_exempt,
    set_guest_bot_block_enabled,
)
from anticheatbot.time_utils import utc_now
from anticheatbot.telegram_retry import retry_telegram

log = logging.getLogger(__name__)

router = Router(name="guest_bots")


async def _has_pending_verification(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    """A live token takes precedence over old user state from an earlier membership."""
    group = await session.get(GroupSettings, chat_id)
    if group is not None and not group.verification_enabled:
        return False
    if member_mute_pending(chat_id, user_id) and await pending_join_needs_verification(session, chat_id, user_id):
        return True
    if member_verification_exempt(chat_id, user_id):
        return False
    stmt = (
        select(VerifyToken.token)
        .where(
            VerifyToken.chat_id == chat_id,
            VerifyToken.user_id == user_id,
            VerifyToken.used_at.is_(None),
            VerifyToken.expires_at > utc_now(),
        )
        .limit(1)
    )
    return (await session.scalar(stmt)) is not None


class _PendingVerificationMessageFilter(BaseFilter):
    """Catch messages sent in the gap before Telegram applies a new member's mute."""

    async def __call__(self, message: Message) -> bool:
        user = message.from_user
        if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return False
        if user is None or user.is_bot or user.id in get_settings().global_admin_id_set:
            return False
        async with session_scope() as session:
            return await _has_pending_verification(session, message.chat.id, user.id)


def _message_is_guest_bot_reply(message: Message) -> bool:
    """The caller fields mark a reply sent by a guest bot, not the user's summon."""
    sender = message.from_user
    return bool(
        sender is not None
        and sender.is_bot
        and (message.guest_bot_caller_user is not None or message.guest_bot_caller_chat is not None)
    )


class _BlockGuestBotsInGroupFilter(BaseFilter):
    """Delete guest replies immediately when blocked, or from a pending caller."""

    async def __call__(self, message: Message) -> bool:
        if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return False
        if not _message_is_guest_bot_reply(message):
            return False
        if guest_bot_block_enabled(message.chat.id):
            return True
        async with session_scope() as session:
            gs = await get_or_create_group_settings(session, message.chat.id)
            if gs.block_guest_bots:
                set_guest_bot_block_enabled(message.chat.id, True)
                return True
            caller = message.guest_bot_caller_user
            return caller is not None and await _has_pending_verification(
                session, message.chat.id, caller.id
            )


async def _delete_guest_bot_spam(bot: Bot, message: Message) -> None:
    try:
        await retry_telegram(lambda: bot.delete_message(message.chat.id, message.message_id))
    except TelegramAPIError as e:
        log.warning("guest bot block: delete failed chat=%s mid=%s: %s", message.chat.id, message.message_id, e)
        return
    uid = message.from_user.id if message.from_user else None
    async with session_scope() as session:
        await log_event(
            session,
            event_type="guest_bot_message_deleted",
            chat_id=message.chat.id,
            user_id=uid,
            payload={"message_id": message.message_id},
        )


@router.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    ~F.new_chat_members,
    ~F.left_chat_member,
    _PendingVerificationMessageFilter(),
)
@router.edited_message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    _PendingVerificationMessageFilter(),
)
async def on_pending_verification_message(message: Message, bot: Bot) -> None:
    try:
        await retry_telegram(lambda: bot.delete_message(message.chat.id, message.message_id))
    except TelegramAPIError as e:
        log.warning(
            "pending verification: delete failed chat=%s mid=%s: %s",
            message.chat.id,
            message.message_id,
            e,
        )
        return
    async with session_scope() as session:
        await log_event(
            session,
            event_type="unverified_message_deleted",
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else None,
            payload={"message_id": message.message_id},
        )


@router.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    _BlockGuestBotsInGroupFilter(),
)
@router.edited_message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    _BlockGuestBotsInGroupFilter(),
)
async def on_group_message_guest_context(message: Message, bot: Bot) -> None:
    await _delete_guest_bot_spam(bot, message)
