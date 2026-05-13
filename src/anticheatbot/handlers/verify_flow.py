from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot, Router, F
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from sqlalchemy import select

from anticheatbot.bot_i18n import BotLocale, bot_t, resolve_bot_locale
from anticheatbot.config import Settings, get_settings
from anticheatbot.db.models import UserChatState, VerifyToken
from anticheatbot.db.session import session_scope
from anticheatbot.services.audit import log_event
from anticheatbot.services.groups import (
    get_or_create_group_settings,
    touch_known_chat,
)
from anticheatbot.telegram_perms import (
    muted_member_permissions,
    normal_member_permissions,
)

log = logging.getLogger(__name__)

router = Router(name="verify_flow")


def _log_verify_send_error(*, channel: str, exc: BaseException) -> None:
    log.warning("send verify message %s failed: %s", channel, exc)
    if isinstance(exc, TelegramBadRequest) and "BUTTON_TYPE_INVALID" in str(exc):
        log.warning(
            "WebApp inline button rejected (BUTTON_TYPE_INVALID). Domain may still be correct: "
            "some chats/clients reject web_app while accepting t.me/?startapp= or plain /admin links. "
            "Ensure WEBAPP_PUBLIC_URL is https and @BotFather Mini App domain matches the host."
        )
    if isinstance(exc, TelegramForbiddenError) and channel == "dm":
        log.warning(
            "DM fallback blocked: user must open a private chat with this bot and send /start once."
        )


_bot_username_lock = asyncio.Lock()
_bot_username_fetched: bool = False
_bot_username_value: str | None = None


async def _cached_bot_username(bot: Bot) -> str | None:
    """Telegram username for t.me/?startapp= fallback (lowercase, no @)."""
    global _bot_username_fetched, _bot_username_value
    async with _bot_username_lock:
        if not _bot_username_fetched:
            me = await bot.get_me()
            u = (me.username or "").strip().lower()
            _bot_username_value = u if u else None
            _bot_username_fetched = True
        return _bot_username_value


def _verify_markup_webapp(*, settings: Settings, token: str, loc: BotLocale) -> InlineKeyboardMarkup:
    url = f"{settings.webapp_public_url}/verify/index.html?t={token}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=bot_t(loc, "btn_open_verify"), web_app=WebAppInfo(url=url)
                )
            ],
        ]
    )


async def _verify_markup_startapp(bot: Bot, token: str, loc: BotLocale) -> InlineKeyboardMarkup | None:
    """Direct-link Mini App (url button); works when inline web_app is rejected."""
    uname = await _cached_bot_username(bot)
    if not uname:
        return None
    if len(token) > 64:
        log.warning("verify token longer than 64 chars; t.me startapp fallback may be rejected")
    link = f"https://t.me/{uname}?startapp={token}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=bot_t(loc, "btn_open_verify"), url=link)],
        ]
    )


async def _send_verify_invite_message(
    bot: Bot,
    *,
    target_chat_id: int,
    text: str,
    settings: Settings,
    token: str,
    loc: BotLocale,
    message_thread_id: int | None,
    channel_label: str,
) -> bool:
    send_kw: dict[str, Any] = {}
    if message_thread_id is not None:
        send_kw["message_thread_id"] = message_thread_id

    kb_web = _verify_markup_webapp(settings=settings, token=token, loc=loc)
    try:
        await bot.send_message(target_chat_id, text, reply_markup=kb_web, **send_kw)
        return True
    except TelegramForbiddenError as e:
        _log_verify_send_error(channel=channel_label, exc=e)
        return False
    except TelegramBadRequest as e:
        _log_verify_send_error(channel=channel_label, exc=e)
        if "BUTTON_TYPE_INVALID" not in str(e):
            return False
        log.info(
            "verify: retrying invite for chat=%s using t.me/?startapp= (direct Mini App link)",
            target_chat_id,
        )
        kb_link = await _verify_markup_startapp(bot, token, loc)
        if kb_link is None:
            log.warning("verify: t.me startapp fallback unavailable (bot has no @username in Telegram)")
            return False
        try:
            await bot.send_message(target_chat_id, text, reply_markup=kb_link, **send_kw)
            return True
        except (TelegramBadRequest, TelegramForbiddenError) as e2:
            _log_verify_send_error(channel=f"{channel_label} (t.me link)", exc=e2)
            return False


async def _delete_unused_verify_token(token: str) -> None:
    async with session_scope() as session:
        vt = await session.get(VerifyToken, token)
        if vt is not None and vt.used_at is None:
            await session.delete(vt)


async def _verify_timeout_task(
    bot: Bot,
    *,
    chat_id: int,
    user_id: int,
    token: str,
    delay: float,
    kick: bool,
) -> None:
    await asyncio.sleep(delay)
    async with session_scope() as session:
        vt = await session.get(VerifyToken, token)
        if vt is None or vt.used_at is not None:
            return
        ucs = await session.get(UserChatState, (chat_id, user_id))
        if ucs is not None and ucs.verified:
            return
        await log_event(
            session,
            event_type="verify_timeout",
            chat_id=chat_id,
            user_id=user_id,
            payload={"token": token[:8]},
        )
    if not kick:
        return
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.warning("kick on verify timeout failed: %s", e)


async def _start_verification_for_member(
    bot: Bot,
    *,
    chat_id: int,
    user_id: int,
    chat_title: str | None,
    user_language_code: str | None = None,
    message_thread_id: int | None = None,
) -> None:
    settings = get_settings()
    async with session_scope() as session:
        stmt = (
            select(VerifyToken)
            .where(
                VerifyToken.chat_id == chat_id,
                VerifyToken.user_id == user_id,
                VerifyToken.used_at.is_(None),
            )
            .order_by(VerifyToken.created_at.desc())
            .limit(1)
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is not None and existing.expires_at > datetime.now(tz=UTC):
            return

        await touch_known_chat(session, chat_id, chat_title)
        if user_id in settings.global_admin_id_set:
            now = datetime.now(tz=UTC)
            ucs = await session.get(UserChatState, (chat_id, user_id))
            if ucs is None:
                session.add(
                    UserChatState(
                        chat_id=chat_id, user_id=user_id, verified=True, verified_at=now
                    )
                )
            else:
                ucs.verified = True
                ucs.verified_at = now
            await log_event(
                session,
                event_type="join_global_admin_bypass",
                chat_id=chat_id,
                user_id=user_id,
            )
            return

        gs = await get_or_create_group_settings(session, chat_id)
        if not gs.verification_enabled:
            await log_event(
                session, event_type="join_no_verify", chat_id=chat_id, user_id=user_id
            )
            return

        token = secrets.token_urlsafe(24)
        expires = datetime.now(tz=UTC) + timedelta(seconds=gs.verify_timeout_seconds)
        session.add(
            VerifyToken(
                token=token, chat_id=chat_id, user_id=user_id, expires_at=expires
            )
        )
        await session.flush()
        await log_event(
            session, event_type="join_verify_started", chat_id=chat_id, user_id=user_id
        )

    try:
        await bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=muted_member_permissions(),
            use_independent_chat_permissions=True,
        )
    except Exception as e:
        log.warning("restrict new member failed: %s", e)
        await _delete_unused_verify_token(token)
        return

    loc = resolve_bot_locale(user_language_code)
    text = bot_t(loc, "verify_invite")
    send_ok = await _send_verify_invite_message(
        bot,
        target_chat_id=chat_id,
        text=text,
        settings=settings,
        token=token,
        loc=loc,
        message_thread_id=message_thread_id,
        channel_label="to group",
    )
    if not send_ok:
        send_ok = await _send_verify_invite_message(
            bot,
            target_chat_id=user_id,
            text=text,
            settings=settings,
            token=token,
            loc=loc,
            message_thread_id=None,
            channel_label="dm",
        )
    if not send_ok:
        await _delete_unused_verify_token(token)
        try:
            await bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=normal_member_permissions(),
                use_independent_chat_permissions=True,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            log.warning("unrestrict after verify send failure failed: %s", e)
        return

    asyncio.create_task(
        _verify_timeout_task(
            bot,
            chat_id=chat_id,
            user_id=user_id,
            token=token,
            delay=float(gs.verify_timeout_seconds),
            kick=gs.kick_on_verify_timeout,
        )
    )


@router.my_chat_member()
async def on_bot_chat_member(event: ChatMemberUpdated, bot: Bot) -> None:
    if event.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if event.new_chat_member.user.id != bot.id:
        return
    if event.new_chat_member.status not in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    ):
        return
    async with session_scope() as session:
        await touch_known_chat(session, event.chat.id, event.chat.title)
        await get_or_create_group_settings(session, event.chat.id)


@router.chat_member(
    ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION),
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
)
async def on_user_joined(event: ChatMemberUpdated, bot: Bot) -> None:
    user = event.new_chat_member.user
    if user.is_bot:
        return
    await _start_verification_for_member(
        bot,
        chat_id=event.chat.id,
        user_id=user.id,
        chat_title=event.chat.title,
        user_language_code=user.language_code,
        message_thread_id=getattr(event, "message_thread_id", None),
    )


@router.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.new_chat_members,
)
async def on_new_chat_members(message: Message, bot: Bot) -> None:
    if not message.new_chat_members:
        return
    for user in message.new_chat_members:
        if user.is_bot:
            continue
        await _start_verification_for_member(
            bot,
            chat_id=message.chat.id,
            user_id=user.id,
            chat_title=message.chat.title,
            user_language_code=user.language_code,
            message_thread_id=message.message_thread_id,
        )
