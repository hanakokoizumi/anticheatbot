from __future__ import annotations

import asyncio
import html
import logging
import secrets
from datetime import timedelta
from typing import Any

from aiogram import Bot, Router, F
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
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
from anticheatbot.kick_registry import register_recent_bot_kick
from anticheatbot.services.audit import log_event
from anticheatbot.services.groups import (
    get_or_create_group_settings,
    touch_known_chat,
)
from anticheatbot.telegram_perms import (
    muted_member_permissions,
    normal_member_permissions,
)
from anticheatbot.time_utils import coerce_utc, utc_now

log = logging.getLogger(__name__)

# Placeholders must survive html.escape (no &, <, > in sentinels).
_WELCOME_PH_M = "\ufdd0MENTION\ufdd1"
_WELCOME_PH_U = "\ufdd0USERNAME\ufdd1"
_WELCOME_PH_I = "\ufdd0USERID\ufdd1"


def post_verify_behavior_normalized(value: str | None) -> str:
    """DB / clients may store trailing spaces or odd casing; only exact 'welcome' enables welcome send."""
    v = str(value or "delete_only").strip().lower()
    return "welcome" if v == "welcome" else "delete_only"


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
) -> Message | None:
    send_kw: dict[str, Any] = {}
    if message_thread_id is not None:
        send_kw["message_thread_id"] = message_thread_id

    kb_web = _verify_markup_webapp(settings=settings, token=token, loc=loc)
    try:
        return await bot.send_message(target_chat_id, text, reply_markup=kb_web, **send_kw)
    except TelegramForbiddenError as e:
        _log_verify_send_error(channel=channel_label, exc=e)
        return None
    except TelegramBadRequest as e:
        _log_verify_send_error(channel=channel_label, exc=e)
        if "BUTTON_TYPE_INVALID" not in str(e):
            return None
        log.info(
            "verify: retrying invite for chat=%s using t.me/?startapp= (direct Mini App link)",
            target_chat_id,
        )
        kb_link = await _verify_markup_startapp(bot, token, loc)
        if kb_link is None:
            log.warning("verify: t.me startapp fallback unavailable (bot has no @username in Telegram)")
            return None
        try:
            return await bot.send_message(target_chat_id, text, reply_markup=kb_link, **send_kw)
        except (TelegramBadRequest, TelegramForbiddenError) as e2:
            _log_verify_send_error(channel=f"{channel_label} (t.me link)", exc=e2)
            return None


async def delete_stored_verify_invite(
    bot: Bot, *, invite_message_chat_id: int | None, invite_message_id: int | None
) -> None:
    """Best-effort delete of the bot-sent verify invite (group or DM)."""
    if invite_message_chat_id is None or invite_message_id is None:
        return
    try:
        await bot.delete_message(invite_message_chat_id, invite_message_id)
    except TelegramBadRequest as e:
        log.warning("delete verify invite message failed: %s", e)


async def delete_stored_verify_join_message(
    bot: Bot, *, join_message_chat_id: int | None, join_message_id: int | None
) -> None:
    """Best-effort delete of the Telegram service join message (new_chat_members)."""
    if join_message_chat_id is None or join_message_id is None:
        return
    try:
        await bot.delete_message(join_message_chat_id, join_message_id)
    except TelegramBadRequest as e:
        log.warning("delete verify join service message failed: %s", e)


async def send_post_verify_welcome(
    bot: Bot,
    *,
    chat_id: int,
    user_id: int,
    template: str,
    message_thread_id: int | None = None,
) -> None:
    """Send welcome text after verification; template may include {mention},{username},{user_id}.

    User content is HTML-escaped before insertion so stray < or & in the template do not break Telegram HTML.
    If HTML send still fails, retries as plain text (no parse mode).
    """
    raw = (template or "").strip()
    if not raw:
        return
    full = str(user_id)
    uname = ""
    try:
        cm = await bot.get_chat_member(chat_id, user_id)
        u = cm.user
        parts = [u.first_name or "", u.last_name or ""]
        full = " ".join(p for p in parts if p).strip() or str(user_id)
        uname = (u.username or "").strip()
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.debug("welcome get_chat_member failed: %s", e)
    mention = f'<a href="tg://user?id={user_id}">{html.escape(full)}</a>'
    username_disp = f"@{html.escape(uname)}" if uname else ""
    body = (
        raw.replace("{mention}", _WELCOME_PH_M)
        .replace("{username}", _WELCOME_PH_U)
        .replace("{user_id}", _WELCOME_PH_I)
    )
    safe = html.escape(body, quote=True)
    text_html = (
        safe.replace(_WELCOME_PH_M, mention)
        .replace(_WELCOME_PH_U, username_disp)
        .replace(_WELCOME_PH_I, html.escape(str(user_id)))
    )
    plain = (
        raw.replace("{mention}", full)
        .replace("{username}", f"@{uname}" if uname else "")
        .replace("{user_id}", str(user_id))
    )
    send_kw: dict[str, Any] = {
        "parse_mode": ParseMode.HTML,
        "disable_web_page_preview": True,
    }
    if message_thread_id is not None:
        send_kw["message_thread_id"] = message_thread_id
    try:
        await bot.send_message(chat_id, text_html, **send_kw)
        return
    except TelegramBadRequest as e:
        log.warning("post-verify welcome HTML send failed, retrying plain: %s", e)
    plain_kw: dict[str, Any] = {"disable_web_page_preview": True}
    if message_thread_id is not None:
        plain_kw["message_thread_id"] = message_thread_id
    try:
        await bot.send_message(chat_id, plain, **plain_kw)
    except TelegramBadRequest as e2:
        log.warning("post-verify welcome plain send failed: %s", e2)


async def _delete_unused_verify_token(token: str) -> None:
    async with session_scope() as session:
        vt = await session.get(VerifyToken, token)
        if vt is not None and vt.used_at is None:
            await session.delete(vt)


def _direct_admin_add_from_chat_member(event: ChatMemberUpdated) -> bool:
    """Member was added by another user (e.g. admin) — not self-join via link / join request."""
    user = event.new_chat_member.user
    actor = event.from_user
    if actor is None or user.is_bot:
        return False
    if actor.id == user.id:
        return False
    if event.via_join_request:
        return False
    if event.invite_link is not None:
        return False
    return True


def _direct_admin_add_from_new_members_message(message: Message, member_user_id: int) -> bool:
    """Service message ``new_chat_members``: ``from_user`` is usually the inviter when admin adds someone."""
    fu = message.from_user
    if fu is None or fu.is_bot:
        return False
    return fu.id != member_user_id


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
    invite_chat_id: int | None = None
    invite_message_id: int | None = None
    join_chat_id: int | None = None
    join_message_id: int | None = None
    async with session_scope() as session:
        vt = await session.get(VerifyToken, token)
        if vt is None or vt.used_at is not None:
            return
        ucs = await session.get(UserChatState, (chat_id, user_id))
        if ucs is not None and ucs.verified:
            return
        invite_chat_id = vt.invite_message_chat_id
        invite_message_id = vt.invite_message_id
        join_chat_id = vt.join_message_chat_id
        join_message_id = vt.join_message_id
        await log_event(
            session,
            event_type="verify_timeout",
            chat_id=chat_id,
            user_id=user_id,
            payload={"token": token[:8]},
        )
    await delete_stored_verify_invite(
        bot, invite_message_chat_id=invite_chat_id, invite_message_id=invite_message_id
    )
    await delete_stored_verify_join_message(
        bot, join_message_chat_id=join_chat_id, join_message_id=join_message_id
    )
    if not kick:
        return
    try:
        await register_recent_bot_kick(chat_id=chat_id, user_id=user_id)
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
    join_message_chat_id: int | None = None,
    join_message_id: int | None = None,
    direct_admin_add: bool = False,
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
        if existing is not None and coerce_utc(existing.expires_at) > utc_now():
            return

        await touch_known_chat(session, chat_id, chat_title)
        if user_id in settings.global_admin_id_set:
            now = utc_now()
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

        if direct_admin_add:
            now = utc_now()
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
                event_type="join_verify_skipped_direct_add",
                chat_id=chat_id,
                user_id=user_id,
            )
            return

        verify_timeout_seconds = int(gs.verify_timeout_seconds)
        kick_on_verify_timeout = bool(gs.kick_on_verify_timeout)

        token = secrets.token_urlsafe(24)
        # Provisional deadline: real expires_at is set again after the invite is sent so
        # it matches the asyncio timeout task (both start when the user can open the link).
        expires = utc_now() + timedelta(seconds=verify_timeout_seconds)
        session.add(
            VerifyToken(
                token=token,
                chat_id=chat_id,
                user_id=user_id,
                expires_at=expires,
                join_message_chat_id=join_message_chat_id,
                join_message_id=join_message_id,
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
    invite_msg = await _send_verify_invite_message(
        bot,
        target_chat_id=chat_id,
        text=text,
        settings=settings,
        token=token,
        loc=loc,
        message_thread_id=message_thread_id,
        channel_label="to group",
    )
    if invite_msg is None:
        invite_msg = await _send_verify_invite_message(
            bot,
            target_chat_id=user_id,
            text=text,
            settings=settings,
            token=token,
            loc=loc,
            message_thread_id=None,
            channel_label="dm",
        )
    if invite_msg is None:
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

    async with session_scope() as session:
        vt_row = await session.get(VerifyToken, token)
        if vt_row is not None:
            vt_row.invite_message_chat_id = invite_msg.chat.id
            vt_row.invite_message_id = invite_msg.message_id
            # Align token expiry with kick timer: both count from invite delivery, not from
            # first DB insert (which can be seconds earlier and made the web UI expire first).
            deadline = utc_now() + timedelta(seconds=verify_timeout_seconds)
            vt_row.expires_at = deadline

    asyncio.create_task(
        _verify_timeout_task(
            bot,
            chat_id=chat_id,
            user_id=user_id,
            token=token,
            delay=float(verify_timeout_seconds),
            kick=kick_on_verify_timeout,
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
    direct = _direct_admin_add_from_chat_member(event)
    await _start_verification_for_member(
        bot,
        chat_id=event.chat.id,
        user_id=user.id,
        chat_title=event.chat.title,
        user_language_code=user.language_code,
        message_thread_id=getattr(event, "message_thread_id", None),
        direct_admin_add=direct,
    )


@router.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.new_chat_members,
)
async def on_new_chat_members(message: Message, bot: Bot) -> None:
    if not message.new_chat_members:
        return
    async with session_scope() as session:
        gs = await get_or_create_group_settings(session, message.chat.id)
        should_del_join = gs.delete_join_message
        verification_on = gs.verification_enabled

    has_human = any(not u.is_bot for u in message.new_chat_members)
    # Immediate delete when enabled (do not defer for verification).
    if should_del_join:
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except TelegramBadRequest as e:
            log.warning("delete join message failed: %s", e)

    # Only record join message for later delete on verify success/timeout when it was not removed above.
    join_c = message.chat.id if verification_on and has_human and not should_del_join else None
    join_m = message.message_id if verification_on and has_human and not should_del_join else None
    for user in message.new_chat_members:
        if user.is_bot:
            continue
        direct = _direct_admin_add_from_new_members_message(message, user.id)
        await _start_verification_for_member(
            bot,
            chat_id=message.chat.id,
            user_id=user.id,
            chat_title=message.chat.title,
            user_language_code=user.language_code,
            message_thread_id=message.message_thread_id,
            join_message_chat_id=join_c,
            join_message_id=join_m,
            direct_admin_add=direct,
        )
