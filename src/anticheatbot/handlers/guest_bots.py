"""Delete spam from Telegram Guest Bots (Bot API guest_query_id / guest_message updates)."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter
from aiogram.types import Message

from anticheatbot.db.session import session_scope
from anticheatbot.services.audit import log_event
from anticheatbot.services.groups import get_or_create_group_settings

log = logging.getLogger(__name__)

router = Router(name="guest_bots")


def _message_has_guest_bot_context(message: Message) -> bool:
    """True when Bot API marks this message as guest-bot / guest-query related."""
    if message.guest_query_id:
        return True
    if message.guest_bot_caller_user is not None:
        return True
    if message.guest_bot_caller_chat is not None:
        return True
    return False


class _BlockGuestBotsInGroupFilter(BaseFilter):
    """Matches group/supergroup messages when group_settings.block_guest_bots is enabled."""

    require_guest_fields: bool

    def __init__(self, *, require_guest_fields: bool) -> None:
        self.require_guest_fields = require_guest_fields

    async def __call__(self, message: Message) -> bool:
        if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return False
        if self.require_guest_fields and not _message_has_guest_bot_context(message):
            return False
        async with session_scope() as session:
            gs = await get_or_create_group_settings(session, message.chat.id)
            return bool(gs.block_guest_bots)


async def _delete_guest_bot_spam(bot: Bot, message: Message) -> None:
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except TelegramBadRequest as e:
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


@router.guest_message(_BlockGuestBotsInGroupFilter(require_guest_fields=False))
async def on_guest_message(message: Message, bot: Bot) -> None:
    await _delete_guest_bot_spam(bot, message)


@router.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    _BlockGuestBotsInGroupFilter(require_guest_fields=True),
)
async def on_group_message_guest_context(message: Message, bot: Bot) -> None:
    await _delete_guest_bot_spam(bot, message)
