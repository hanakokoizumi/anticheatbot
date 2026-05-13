from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from anticheatbot.bot_i18n import BotLocale, bot_t, resolve_bot_locale
from anticheatbot.config import get_settings

router = Router(name="admin_cmds")


def _admin_locale(message: Message) -> BotLocale:
    u = message.from_user
    return resolve_bot_locale(u.language_code if u else None)


@router.message(
    Command("admin"), F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP])
)
async def cmd_admin_group(message: Message) -> None:
    settings = get_settings()
    loc = _admin_locale(message)
    url = f"{settings.webapp_public_url}/admin/index.html?chat_id={message.chat.id}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=bot_t(loc, "btn_open_admin"), web_app=WebAppInfo(url=url)
                )
            ]
        ]
    )
    await message.answer(bot_t(loc, "cmd_admin_group"), reply_markup=kb)


@router.message(Command("admin"), F.chat.type == ChatType.PRIVATE)
async def cmd_admin_private(message: Message) -> None:
    settings = get_settings()
    loc = _admin_locale(message)
    url = f"{settings.webapp_public_url}/admin/index.html"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=bot_t(loc, "btn_open_admin"), web_app=WebAppInfo(url=url)
                )
            ]
        ]
    )
    await message.answer(bot_t(loc, "cmd_admin_private"), reply_markup=kb)
