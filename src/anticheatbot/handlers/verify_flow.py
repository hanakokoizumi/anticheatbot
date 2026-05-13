from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta

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

from anticheatbot.bot_i18n import bot_t, resolve_bot_locale
from anticheatbot.config import get_settings
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
    url = f"{settings.webapp_public_url}/verify/index.html?t={token}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=bot_t(loc, "btn_open_verify"), web_app=WebAppInfo(url=url)
                )
            ],
        ]
    )
    text = bot_t(loc, "verify_invite")
    send_ok = False
    try:
        await bot.send_message(chat_id, text, reply_markup=kb)
        send_ok = True
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.warning("send verify message to group failed: %s", e)
    if not send_ok:
        try:
            await bot.send_message(user_id, text, reply_markup=kb)
            send_ok = True
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            log.warning("send verify message via DM failed: %s", e)
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
        )
