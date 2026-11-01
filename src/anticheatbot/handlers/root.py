from __future__ import annotations

import re

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import Message

from anticheatbot.bot_i18n import bot_t, resolve_bot_locale
from anticheatbot.handlers.admin_cmds import send_group_admin_in_private

router = Router(name="root")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if message.chat.type == ChatType.PRIVATE and len(parts) == 2:
        match = re.fullmatch(r"admin_(-[0-9]{1,19})", parts[1].strip())
        if match is not None and -(2**63) <= int(match.group(1)) < 0:
            await send_group_admin_in_private(message, int(match.group(1)))
            return
    lc = message.from_user.language_code if message.from_user else None
    loc = resolve_bot_locale(lc)
    await message.answer(bot_t(loc, "cmd_start"))
