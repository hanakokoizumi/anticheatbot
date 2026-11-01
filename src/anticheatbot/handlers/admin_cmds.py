from __future__ import annotations

from aiogram import Bot, F, Router
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
from anticheatbot.handlers.verify_flow import _cached_bot_username

router = Router(name="admin_cmds")


def _admin_locale(message: Message) -> BotLocale:
    u = message.from_user
    return resolve_bot_locale(u.language_code if u else None)


@router.message(
    Command("admin"), F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP])
)
async def cmd_admin_group(message: Message, bot: Bot) -> None:
    loc = _admin_locale(message)
    username = await _cached_bot_username(bot)
    if not username:
        return
    # Telegram permits inline WebApp buttons only in private chats. Preserve the group
    # target through /start, where the bot can send a supported button after the user opens it.
    url = f"https://t.me/{username}?start=admin_{message.chat.id}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=bot_t(loc, "btn_open_admin"), url=url
                )
            ]
        ]
    )
    await message.answer(bot_t(loc, "cmd_admin_group"), reply_markup=kb)


async def send_group_admin_in_private(message: Message, chat_id: int) -> None:
    settings = get_settings()
    loc = _admin_locale(message)
    url = f"{settings.webapp_public_url}/admin/group.html?chat_id={chat_id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=bot_t(loc, "btn_open_admin"), web_app=WebAppInfo(url=url),
    )]])
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
