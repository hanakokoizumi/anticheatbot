from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from anticheatbot.bot_i18n import bot_t, resolve_bot_locale

router = Router(name="root")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    lc = message.from_user.language_code if message.from_user else None
    loc = resolve_bot_locale(lc)
    await message.answer(bot_t(loc, "cmd_start"))
