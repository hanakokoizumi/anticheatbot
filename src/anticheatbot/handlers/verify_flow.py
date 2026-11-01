from __future__ import annotations

import asyncio
import html
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from weakref import WeakValueDictionary

from aiogram import Bot, Router, F
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    WebAppInfo,
)
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from anticheatbot.bot_i18n import BotLocale, bot_t, resolve_bot_locale
from anticheatbot.config import Settings, get_settings
from anticheatbot.db.models import GroupSettings, UserChatState, VerifyToken
from anticheatbot.db.session import session_scope
from anticheatbot.kick_registry import register_recent_bot_kick
from anticheatbot.services.audit import log_event
from anticheatbot.services.groups import (
    get_or_create_group_settings,
    member_verification_exempt,
    set_member_mute_pending,
    set_member_verification_exempt,
    touch_known_chat,
)
from anticheatbot.telegram_perms import (
    muted_member_permissions,
    normal_member_permissions,
)
from anticheatbot.time_utils import coerce_utc, utc_now
from anticheatbot.telegram_retry import retry_telegram

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
_member_locks: WeakValueDictionary[tuple[int, int], asyncio.Lock] = WeakValueDictionary()
_verification_tasks: set[asyncio.Task] = set()
_verification_tasks_by_token: dict[str, asyncio.Task] = {}
_active_verification_tasks: set[asyncio.Task] = set()
_immediate_verification_tasks: set[asyncio.Task] = set()
_timeout_semaphore = asyncio.Semaphore(4)
_VERIFY_PREPARATION_SECONDS = 180


class _MembershipVersion:
    update_id: int | None = None
    event_at: datetime | None = None
    users: int = 0


_membership_versions: WeakValueDictionary[tuple[int, int], _MembershipVersion] = WeakValueDictionary()


@dataclass(eq=False)
class _PendingJoin:
    source: int
    event_at: datetime | None
    update_id: int | None
    cancelled: bool = False


_pending_join_events: dict[tuple[int, int], list[_PendingJoin]] = {}
_pending_membership_retirements: dict[tuple[int, int], _PendingJoin] = {}


def _begin_pending_join(chat_id: int, user_id: int, *, source: int, event_at: datetime | None, update_id: int | None) -> _PendingJoin:
    notice = _PendingJoin(source, event_at, update_id)
    _pending_join_events.setdefault((chat_id, user_id), []).append(notice)
    set_member_mute_pending(chat_id, user_id, True)
    return notice


def _end_pending_join(chat_id: int, user_id: int, notice: _PendingJoin) -> None:
    key = (chat_id, user_id)
    notices = _pending_join_events.get(key, [])
    if notice in notices:
        notices.remove(notice)
    if not notices:
        _pending_join_events.pop(key, None)
    set_member_mute_pending(chat_id, user_id, False)


def _membership_current(chat_id: int, user_id: int, version: _MembershipVersion) -> bool:
    return _membership_versions.get((chat_id, user_id)) is version and not member_verification_exempt(chat_id, user_id)


def _member_lock(chat_id: int, user_id: int) -> asyncio.Lock:
    return _member_locks.setdefault((chat_id, user_id), asyncio.Lock())


def _event_precedes(previous_at: datetime | None, previous_id: int | None, event_at: datetime | None, update_id: int | None) -> bool:
    if previous_at is not None and event_at is not None:
        previous_at, event_at = coerce_utc(previous_at), coerce_utc(event_at)
        if event_at != previous_at:
            # Telegram can choose a new random update ID after a week without updates.
            return event_at < previous_at
    return update_id is not None and previous_id is not None and update_id <= previous_id


def _membership_event_stale(state: UserChatState, event_at: datetime | None, update_id: int | None) -> bool:
    return _event_precedes(state.membership_event_at, state.membership_update_id, event_at, update_id)


def _join_event_duplicate(
    state: UserChatState, *, source: int, event_at: datetime | None, update_id: int | None,
    legacy_token_created_at: datetime | None = None,
) -> bool:
    if _membership_event_stale(state, event_at, update_id):
        return True
    seen = state.membership_join_sources or 0
    same_event_time = event_at is not None and state.membership_event_at is not None and (
        coerce_utc(event_at) == coerce_utc(state.membership_event_at)
    )
    duplicate = (
        state.membership_status == "joined" and not seen & source and same_event_time
    )
    # Old consumed attempts need the same replay protection as successful ones.
    if state.membership_status is None and event_at:
        baseline = legacy_token_created_at or (state.verified_at if state.verified else None)
        if baseline is not None:
            duplicate = coerce_utc(event_at) <= coerce_utc(baseline)
    return duplicate


def _record_join_lifecycle(
    state: UserChatState, *, source: int, event_at: datetime | None, update_id: int | None,
    legacy_token_created_at: datetime | None = None,
) -> bool:
    """Return False for an older event or the other notice of this same membership."""
    if _membership_event_stale(state, event_at, update_id):
        return False
    duplicate = _join_event_duplicate(
        state, source=source, event_at=event_at, update_id=update_id,
        legacy_token_created_at=legacy_token_created_at,
    )
    seen = state.membership_join_sources or 0
    state.membership_join_sources = (seen | source) if duplicate else source
    state.membership_status = "joined"
    state.membership_update_id = update_id
    state.membership_event_at = event_at
    return not duplicate


async def pending_join_needs_verification(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    """A delayed notice must not temporarily put an approved member back behind the guard."""
    notices = _pending_join_events.get((chat_id, user_id))
    if not notices:
        return not member_verification_exempt(chat_id, user_id)
    notices = [notice for notice in notices if not notice.cancelled]
    if not notices:
        return False
    state = await session.get(UserChatState, (chat_id, user_id))
    if state is None:
        return True
    legacy_created_at = None
    if state.membership_status is None:
        legacy_created_at = await session.scalar(select(VerifyToken.created_at).where(
            VerifyToken.chat_id == chat_id, VerifyToken.user_id == user_id,
        ).order_by(VerifyToken.created_at.desc()).limit(1))
    return any(not notice.cancelled and not _join_event_duplicate(
        state, source=notice.source, event_at=notice.event_at, update_id=notice.update_id,
        legacy_token_created_at=legacy_created_at,
    ) for notice in notices)


def _timeout_retry_delay(error: BaseException) -> float:
    return max(60.0, float(error.retry_after) + 0.1) if isinstance(error, TelegramRetryAfter) else 60.0


def schedule_verification_timeout(bot: Bot, **kwargs: Any) -> None:
    token = kwargs["token"]
    previous = _verification_tasks_by_token.get(token)
    if previous is not None and previous is not asyncio.current_task():
        if not previous.done() and previous in _immediate_verification_tasks and kwargs["delay"] > 0:
            # A settings change may have queued immediate recovery during the join's
            # final commit. Invite delivery must not postpone that work again.
            return
        if previous in _active_verification_tasks:
            # Its final action rechecks current settings; do not interrupt an in-flight
            # Telegram permission request or enqueue unlimited duplicate recovery tasks.
            return
        previous.cancel()
    task = asyncio.create_task(_verify_timeout_task(bot, **kwargs))
    _verification_tasks.add(task)
    _verification_tasks_by_token[token] = task
    if kwargs["delay"] <= 0:
        _immediate_verification_tasks.add(task)

    def finished(done: asyncio.Task) -> None:
        _verification_tasks.discard(done)
        _immediate_verification_tasks.discard(done)
        if _verification_tasks_by_token.get(token) is done:
            _verification_tasks_by_token.pop(token, None)
        if not done.cancelled() and (exc := done.exception()) is not None:
            log.error("verification timeout task failed", exc_info=(type(exc), exc, exc.__traceback__))

    task.add_done_callback(finished)


async def stop_verification_timeouts() -> None:
    tasks = list(_verification_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def restore_verification_timeouts(bot: Bot) -> int:
    """Restore the latest unfinished verification per member after a restart."""
    ranked = select(
        VerifyToken.token,
        func.row_number().over(
            partition_by=(VerifyToken.chat_id, VerifyToken.user_id),
            order_by=VerifyToken.created_at.desc(),
        ).label("rank"),
    ).subquery()
    async with session_scope() as session:
        rows = (await session.execute(
            select(VerifyToken, GroupSettings.kick_on_verify_timeout, GroupSettings.verification_enabled)
            .join(ranked, ranked.c.token == VerifyToken.token)
            .join(GroupSettings, GroupSettings.chat_id == VerifyToken.chat_id)
            .where(ranked.c.rank == 1, VerifyToken.used_at.is_(None))
        )).all()
    for vt, kick, enabled in rows:
        schedule_verification_timeout(
            bot, chat_id=vt.chat_id, user_id=vt.user_id, token=vt.token,
            delay=max(0, (coerce_utc(vt.expires_at) - utc_now()).total_seconds()) if enabled else 0, kick=kick,
        )
    return len(rows)


async def schedule_disabled_verifications(bot: Bot, chat_id: int) -> int:
    """Reuse durable token recovery and its bounded worker pool after a group disables verification."""
    async with session_scope() as session:
        rows = (await session.scalars(select(VerifyToken).where(
            VerifyToken.chat_id == chat_id, VerifyToken.used_at.is_(None),
        ))).all()
    for token in rows:
        schedule_verification_timeout(bot, chat_id=chat_id, user_id=token.user_id, token=token.token, delay=0, kick=False)
    return len(rows)


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

    # Telegram only permits web_app buttons in private chats.
    try:
        kb_web = (
            await _verify_markup_startapp(bot, token, loc)
            if target_chat_id < 0 else _verify_markup_webapp(settings=settings, token=token, loc=loc)
        )
    except TelegramAPIError as e:
        _log_verify_send_error(channel=channel_label, exc=e)
        return None
    if kb_web is None:
        return None
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
        try:
            kb_link = await _verify_markup_startapp(bot, token, loc)
            if kb_link is None:
                log.warning("verify: t.me startapp fallback unavailable (bot has no @username in Telegram)")
                return None
            return await bot.send_message(target_chat_id, text, reply_markup=kb_link, **send_kw)
        except TelegramAPIError as e2:
            _log_verify_send_error(channel=f"{channel_label} (t.me link)", exc=e2)
            return None
    except TelegramAPIError as e:
        _log_verify_send_error(channel=channel_label, exc=e)
        return None


async def delete_stored_verify_invite(
    bot: Bot, *, invite_message_chat_id: int | None, invite_message_id: int | None
) -> None:
    """Best-effort delete of the bot-sent verify invite (group or DM)."""
    if invite_message_chat_id is None or invite_message_id is None:
        return
    try:
        await bot.delete_message(invite_message_chat_id, invite_message_id)
    except TelegramAPIError as e:
        if isinstance(e, TelegramBadRequest) and "message to delete not found" in str(e).lower():
            log.debug("verify invite already deleted")
        else:
            log.warning("delete verify invite message failed: %s", e)


async def delete_stored_verify_join_message(
    bot: Bot, *, join_message_chat_id: int | None, join_message_id: int | None
) -> None:
    """Best-effort delete of the Telegram service join message (new_chat_members)."""
    if join_message_chat_id is None or join_message_id is None:
        return
    try:
        await bot.delete_message(join_message_chat_id, join_message_id)
    except TelegramAPIError as e:
        if isinstance(e, TelegramBadRequest) and "message to delete not found" in str(e).lower():
            log.debug("verify join message already deleted")
        else:
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
    except TelegramAPIError as e:
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
    except TelegramAPIError as e:
        log.warning("post-verify welcome send failed: %s", e)
        return
    plain_kw: dict[str, Any] = {"disable_web_page_preview": True}
    if message_thread_id is not None:
        plain_kw["message_thread_id"] = message_thread_id
    try:
        await bot.send_message(chat_id, plain, **plain_kw)
    except TelegramAPIError as e2:
        log.warning("post-verify welcome plain send failed: %s", e2)


async def _release_after_invite_failure(bot: Bot, token: str) -> bool:
    """Keep the existing release policy, but never discard recovery state before release succeeds."""
    async with session_scope() as session:
        vt = await session.get(VerifyToken, token, with_for_update=True)
        if vt is None or vt.used_at is not None:
            return True
        try:
            current, _ = await verification_is_current(session, vt)
            if not current:
                return False
            member = await _get_verification_member(bot, vt.chat_id, vt.user_id)
            current, state = await verification_is_current(session, vt)
            if not current:
                return False
            present = member is not None and (
                member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
                or (member.status == ChatMemberStatus.RESTRICTED and member.is_member)
            )
            if member is not None and member.status == ChatMemberStatus.RESTRICTED and member.is_member:
                if not verification_state_is_current(vt, state):
                    return False
                await bot.restrict_chat_member(
                    vt.chat_id, vt.user_id, permissions=normal_member_permissions(),
                    use_independent_chat_permissions=True,
                )
        except TelegramAPIError as e:
            log.warning("unrestrict after verify send failure failed: %s", e)
            return False
        vt.used_at = utc_now()
        ucs = await session.get(UserChatState, (vt.chat_id, vt.user_id))
        if ucs is not None and present:
            ucs.verified = True
            ucs.verified_at = utc_now()
        await log_event(session, event_type="verify_invite_failed_released", chat_id=vt.chat_id, user_id=vt.user_id)
    return True


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


async def _inviter_is_admin(bot: Bot, chat_id: int, inviter_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, inviter_id)
    except Exception as e:
        log.warning("cannot verify inviter privileges chat=%s user=%s: %s", chat_id, inviter_id, e)
        return False
    return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)


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
    async with _timeout_semaphore:
        async with _member_lock(chat_id, user_id):
            task = asyncio.current_task()
            _active_verification_tasks.add(task)
            try:
                await _expire_verification(bot, chat_id=chat_id, user_id=user_id, token=token, kick=kick)
            except (TelegramAPIError, SQLAlchemyError) as e:
                log.warning("verification timeout will retry chat=%s user=%s: %s", chat_id, user_id, e)
                schedule_verification_timeout(
                    bot, chat_id=chat_id, user_id=user_id, token=token, delay=_timeout_retry_delay(e), kick=kick
                )
            finally:
                _active_verification_tasks.discard(task)


async def _get_verification_member(bot: Bot, chat_id: int, user_id: int) -> Any | None:
    try:
        return await bot.get_chat_member(chat_id, user_id)
    except TelegramBadRequest as e:
        message = str(e).lower()
        if "member not found" in message or "user not found" in message:
            log.info("verification member no longer exists chat=%s user=%s", chat_id, user_id)
            return None
        raise


def member_lifecycle_is_current(
    chat_id: int, user_id: int, state: UserChatState | None, *, legacy_started_at: datetime | None = None,
) -> bool:
    """Membership guard shared by verification and a verdict for an approved member."""
    newer_join = any(
        not notice.cancelled and (state is None or not _join_event_duplicate(
            state, source=notice.source, event_at=notice.event_at, update_id=notice.update_id,
            legacy_token_created_at=legacy_started_at,
        )) for notice in _pending_join_events.get((chat_id, user_id), [])
    )
    retiring = _pending_membership_retirements.get((chat_id, user_id))
    newer_retirement = retiring is not None and (
        state is None or not _membership_event_stale(state, retiring.event_at, retiring.update_id)
    )
    return not newer_join and not newer_retirement and (state is None or state.membership_status not in ("left", "admin"))


def verification_state_is_current(vt: VerifyToken, state: UserChatState | None, *, allow_verified: bool = False) -> bool:
    """Synchronous final guard after the last awaited read and before a permission request."""
    return member_lifecycle_is_current(vt.chat_id, vt.user_id, state, legacy_started_at=vt.created_at) and not (state is not None and (
        (state.verified and not allow_verified)
        or (state.membership_event_at is not None and coerce_utc(state.membership_event_at) > coerce_utc(vt.created_at))
    ))


async def verification_is_current(
    session: AsyncSession, vt: VerifyToken, *, allow_verified: bool = False,
) -> tuple[bool, UserChatState | None]:
    """Caller owns the token lock; a completion may allow its own verified write."""
    latest = await session.scalar(select(VerifyToken.token).where(
        VerifyToken.chat_id == vt.chat_id, VerifyToken.user_id == vt.user_id,
    ).order_by(VerifyToken.created_at.desc()).limit(1))
    state = await session.get(UserChatState, (vt.chat_id, vt.user_id), populate_existing=True)
    current = latest == vt.token and verification_state_is_current(vt, state, allow_verified=allow_verified)
    return current, state


async def release_disabled_verification(session: AsyncSession, bot: Bot, vt: VerifyToken, *, consumed_here: bool = False) -> bool:
    """Called with the token row locked; return False if verification was enabled again."""
    gs = await session.get(GroupSettings, vt.chat_id, populate_existing=True)
    if gs is None or gs.verification_enabled:
        return False
    if vt.used_at is not None and not consumed_here:
        return True
    current, state = await verification_is_current(session, vt)
    if not current:
        vt.used_at = utc_now()
        return True
    member = await _get_verification_member(bot, vt.chat_id, vt.user_id)
    # A settings save must not wait for Telegram. Recheck after this external read,
    # immediately before issuing any permission change.
    await session.refresh(gs)
    if gs.verification_enabled:
        return False
    current, state = await verification_is_current(session, vt)
    if not current:
        vt.used_at = utc_now()
        return True
    # Check policy after DB reads, then check in-flight membership events synchronously
    # so no awaited operation separates the final decision from Telegram dispatch.
    await session.refresh(gs)
    if gs.verification_enabled:
        return False
    if not verification_state_is_current(vt, state):
        vt.used_at = utc_now()
        return True
    present = member is not None and (
        member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
        or (member.status == ChatMemberStatus.RESTRICTED and member.is_member)
    )
    if member is not None and member.status == ChatMemberStatus.RESTRICTED and member.is_member:
        await bot.restrict_chat_member(
            vt.chat_id, vt.user_id, permissions=normal_member_permissions(), use_independent_chat_permissions=True,
        )
    vt.used_at = utc_now()
    if present:
        if state is None:
            state = UserChatState(chat_id=vt.chat_id, user_id=vt.user_id)
            session.add(state)
        state.verified = True
        state.verified_at = utc_now()
        state.llm_messages_seen = 0
    await log_event(session, event_type="verification_disabled", chat_id=vt.chat_id, user_id=vt.user_id)
    return True


async def _expire_verification(
    bot: Bot, *, chat_id: int, user_id: int, token: str, kick: bool
) -> None:
    invite_chat_id: int | None = None
    invite_message_id: int | None = None
    join_chat_id: int | None = None
    join_message_id: int | None = None
    async with session_scope() as session:
        vt = await session.get(VerifyToken, token, with_for_update=True)
        if vt is None or vt.used_at is not None:
            return
        # A newer membership must never be punished by an older timer.
        latest = await session.scalar(
            select(VerifyToken.token).where(
                VerifyToken.chat_id == chat_id, VerifyToken.user_id == user_id,
            ).order_by(VerifyToken.created_at.desc()).limit(1)
        )
        if latest != token:
            vt.used_at = utc_now()
            return
        gs = await get_or_create_group_settings(session, chat_id)
        disabled = not gs.verification_enabled and await release_disabled_verification(session, bot, vt)
        remaining = (coerce_utc(vt.expires_at) - utc_now()).total_seconds()
        if not disabled and remaining > 0:
            schedule_verification_timeout(
                bot, chat_id=chat_id, user_id=user_id, token=token, delay=remaining, kick=kick
            )
            return
        invite_chat_id = vt.invite_message_chat_id
        invite_message_id = vt.invite_message_id
        join_chat_id = vt.join_message_chat_id
        join_message_id = vt.join_message_id
        ucs = await session.get(UserChatState, (chat_id, user_id))
        should_kick = bool(not disabled and gs.verification_enabled and gs.kick_on_verify_timeout)
        if ucs is not None and ucs.verified:
            should_kick = False
        if should_kick:
            try:
                member = await _get_verification_member(bot, chat_id, user_id)
            except Exception as e:
                log.warning("timeout membership check failed chat=%s user=%s: %s", chat_id, user_id, e)
                schedule_verification_timeout(
                    bot, chat_id=chat_id, user_id=user_id, token=token, delay=_timeout_retry_delay(e), kick=kick
                )
                return
            should_kick = member is not None and member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED)
            if member is not None and member.status == ChatMemberStatus.RESTRICTED and not member.is_member:
                should_kick = False
            current, current_state = await verification_is_current(session, vt)
            should_kick = should_kick and current
            await session.refresh(gs)
            if not gs.verification_enabled:
                disabled = await release_disabled_verification(session, bot, vt)
                if not disabled:
                    schedule_verification_timeout(bot, chat_id=chat_id, user_id=user_id, token=token, delay=60, kick=kick)
                    return
                should_kick = False
            elif not gs.kick_on_verify_timeout:
                should_kick = False
            if not verification_state_is_current(vt, current_state):
                should_kick = False
        vt.used_at = utc_now()
        if should_kick:
            try:
                await register_recent_bot_kick(chat_id=chat_id, user_id=user_id)
                if not verification_state_is_current(vt, current_state):
                    return
                # A failed unban must not turn a kick into a permanent ban.
                await bot.ban_chat_member(chat_id, user_id, until_date=utc_now() + timedelta(seconds=60))
                await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
            except (TelegramBadRequest, TelegramForbiddenError) as e:
                log.warning("kick on verify timeout failed: %s", e)
        if not disabled:
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


async def _mute_pending_token(bot: Bot, *, token: str, version: _MembershipVersion) -> bool:
    async with session_scope() as session:
        vt = await session.get(VerifyToken, token, with_for_update=True)
        if vt is None or vt.used_at is not None:
            return False
        if await release_disabled_verification(session, bot, vt):
            return False
        if not _membership_current(vt.chat_id, vt.user_id, version):
            vt.used_at = utc_now()
            return False
        if coerce_utc(vt.expires_at) <= utc_now():
            return False
        current, state = await verification_is_current(session, vt)
        if not current:
            vt.used_at = utc_now()
            return False
        member = await _get_verification_member(bot, vt.chat_id, vt.user_id)
        current, state = await verification_is_current(session, vt)
        if not current:
            vt.used_at = utc_now()
            return False
        if await release_disabled_verification(session, bot, vt):
            return False
        absent = member is None or member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
        if member is not None and member.status == ChatMemberStatus.RESTRICTED and not member.is_member:
            absent = True
        admin = member is not None and member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
        if absent or admin or not _membership_current(vt.chat_id, vt.user_id, version) or not verification_state_is_current(vt, state):
            vt.used_at = utc_now()
            if admin and state is not None:
                state.verified = True
                state.verified_at = utc_now()
            return False
        if coerce_utc(vt.expires_at) <= utc_now():
            return False
        # Serialize with HTTP completion so a delayed retry cannot re-mute an approved member.
        await bot.restrict_chat_member(
            vt.chat_id, vt.user_id, permissions=muted_member_permissions(), use_independent_chat_permissions=True,
        )
        # Never cancel an already dispatched permission RPC. If verification was
        # disabled while it was in flight, restore immediately before any invitation.
        if await release_disabled_verification(session, bot, vt):
            return False
        return True


async def _join_invitation_still_needed(bot: Bot, token: str, version: _MembershipVersion) -> bool:
    try:
        async with session_scope() as session:
            vt = await session.get(VerifyToken, token, with_for_update=True)
            if vt is None or vt.used_at is not None or not _membership_current(vt.chat_id, vt.user_id, version):
                return False
            current, _ = await verification_is_current(session, vt)
            if not current:
                return False
            return not await release_disabled_verification(session, bot, vt)
    except TelegramAPIError as exc:
        # Persisted timeout recovery will retry; a disabled group must not receive a
        # fresh quiz invitation just because permission restoration was unavailable.
        log.warning("join release remains recoverable token=%s: %s", token[:8], exc)
        return False


async def _start_verification_for_member(bot: Bot, **kwargs: Any) -> None:
    chat_id, user_id = kwargs["chat_id"], kwargs["user_id"]
    version = _membership_versions.setdefault((chat_id, user_id), _MembershipVersion())
    update_id = kwargs.get("membership_update_id")
    event_at = kwargs.get("membership_event_at")
    if _event_precedes(version.event_at, version.update_id, event_at, update_id):
        return
    version.update_id, version.event_at = update_id, event_at
    version.users += 1
    set_member_mute_pending(chat_id, user_id, True)
    try:
        async with _member_lock(chat_id, user_id):
            if _membership_versions.get((chat_id, user_id)) is version:
                await _start_verification_for_member_locked(bot, membership_version=version, **kwargs)
    finally:
        set_member_mute_pending(chat_id, user_id, False)
        version.users -= 1
        if version.users == 0 and _membership_versions.get((chat_id, user_id)) is version:
            _membership_versions.pop((chat_id, user_id), None)


async def _start_verification_for_member_locked(
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
    membership_version: _MembershipVersion,
    membership_source: int = 0,
    membership_event_at: datetime | None = None,
    membership_update_id: int | None = None,
) -> None:
    settings = get_settings()
    async with session_scope() as session:
        # All paths that create both group rows acquire them in this order.
        # Reversing it can deadlock with bot membership/admin setup in a new group.
        await touch_known_chat(session, chat_id, chat_title)
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
        existing = (await session.execute(stmt.with_for_update())).scalar_one_or_none()
        gs = await get_or_create_group_settings(session, chat_id)
        ucs = await session.get(UserChatState, (chat_id, user_id))
        if _membership_versions.get((chat_id, user_id)) is not membership_version:
            return
        new_membership = False
        if membership_source:
            if ucs is None:
                ucs = UserChatState(chat_id=chat_id, user_id=user_id, verified=False)
                session.add(ucs)
                await session.flush()
            if _membership_event_stale(ucs, membership_event_at, membership_update_id):
                set_member_verification_exempt(chat_id, user_id, ucs.membership_status == "admin")
                return
            had_lifecycle = ucs.membership_status is not None
            legacy_created_at = None
            if not had_lifecycle:
                legacy_created_at = await session.scalar(select(VerifyToken.created_at).where(
                    VerifyToken.chat_id == chat_id, VerifyToken.user_id == user_id,
                ).order_by(VerifyToken.created_at.desc()).limit(1))
            fresh = _record_join_lifecycle(
                ucs, source=membership_source, event_at=membership_event_at, update_id=membership_update_id,
                legacy_token_created_at=legacy_created_at,
            )
            set_member_verification_exempt(chat_id, user_id, False)
            new_membership = fresh and had_lifecycle
            if not fresh:
                if existing is not None and join_message_id is not None:
                    existing.join_message_chat_id = join_message_chat_id
                    existing.join_message_id = join_message_id
                return
        if existing is not None and coerce_utc(existing.expires_at) > utc_now() and gs.verification_enabled and not new_membership:
            if join_message_id is not None:
                existing.join_message_chat_id = join_message_chat_id
                existing.join_message_id = join_message_id
            return
        # Retire earlier attempts while holding their row lock, shared with HTTP submit.
        old_tokens = (await session.scalars(
            select(VerifyToken).where(
                VerifyToken.chat_id == chat_id, VerifyToken.user_id == user_id,
                VerifyToken.used_at.is_(None),
            ).with_for_update()
        )).all()
        for old_token in old_tokens:
            old_token.used_at = utc_now()

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
            # Retiring old tokens/lifecycle writes precede the shared refreshed read.
            # Re-enabling during its member RPC falls through to a fresh verification.
            if existing is not None:
                await session.flush()
                disabled = await release_disabled_verification(session, bot, existing, consumed_here=True)
            else:
                await session.refresh(gs)
                disabled = not gs.verification_enabled
            if disabled:
                now = utc_now()
                ucs = await session.get(UserChatState, (chat_id, user_id))
                if ucs is None:
                    session.add(UserChatState(chat_id=chat_id, user_id=user_id, verified=True, verified_at=now))
                else:
                    ucs.verified = True
                    ucs.verified_at = now
                    ucs.llm_messages_seen = 0
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
        ucs = await session.get(UserChatState, (chat_id, user_id))
        if not _membership_current(chat_id, user_id, membership_version):
            return
        token = secrets.token_urlsafe(24)
        # Bind creation to this membership even when Telegram's event timestamp is
        # ahead of the local clock. Explicit microseconds also disambiguate rapid joins.
        created_at = utc_now()
        if membership_event_at is not None:
            created_at = max(created_at, coerce_utc(membership_event_at))
        previous_created_at = await session.scalar(select(func.max(VerifyToken.created_at)).where(
            VerifyToken.chat_id == chat_id, VerifyToken.user_id == user_id,
        ))
        if previous_created_at is not None:
            created_at = max(created_at, coerce_utc(previous_created_at) + timedelta(microseconds=1))
        # Persist recovery before any permission request. The in-memory pending guard
        # already covers this DB work; restart/cancellation can no longer strand a mute.
        expires = utc_now() + timedelta(seconds=verify_timeout_seconds + _VERIFY_PREPARATION_SECONDS)
        if ucs is None:
            session.add(UserChatState(chat_id=chat_id, user_id=user_id, verified=False))
        else:
            ucs.verified = False
            ucs.verified_at = None
            ucs.llm_messages_seen = 0
        session.add(VerifyToken(
            token=token, chat_id=chat_id, user_id=user_id, created_at=created_at, expires_at=expires,
            join_message_chat_id=join_message_chat_id, join_message_id=join_message_id,
        ))
        await session.flush()
        await log_event(session, event_type="join_verify_started", chat_id=chat_id, user_id=user_id)

    # Schedule as soon as recovery state exists. Invite delivery or later DB writes
    # can fail; the member still needs a tracked timeout in that case.
    schedule_verification_timeout(
        bot, chat_id=chat_id, user_id=user_id, token=token,
        delay=float(verify_timeout_seconds + _VERIFY_PREPARATION_SECONDS), kick=kick_on_verify_timeout,
    )

    try:
        active = await retry_telegram(lambda: _mute_pending_token(bot, token=token, version=membership_version))
        if not active:
            return
    except TelegramAPIError as e:
        log.warning("restrict retries exhausted; verification remains recoverable chat=%s user=%s: %s", chat_id, user_id, e)
    if not await _join_invitation_still_needed(bot, token, membership_version):
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
        if not await _join_invitation_still_needed(bot, token, membership_version):
            return
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
        if not await _join_invitation_still_needed(bot, token, membership_version):
            return
        await _release_after_invite_failure(bot, token)
        return

    async with session_scope() as session:
        vt_row = await session.get(VerifyToken, token, with_for_update=True)
        active = vt_row is not None and vt_row.used_at is None and _membership_current(chat_id, user_id, membership_version)
        if active:
            active, _ = await verification_is_current(session, vt_row)
        if active:
            vt_row.invite_message_chat_id = invite_msg.chat.id
            vt_row.invite_message_id = invite_msg.message_id
            try:
                if await release_disabled_verification(session, bot, vt_row):
                    active = False
            except TelegramAPIError as exc:
                # Keep the token and cleanup IDs for queued recovery, without replacing
                # its immediate wakeup with another full verification deadline.
                log.warning("post-invite release remains recoverable token=%s: %s", token[:8], exc)
                active = False
        if active:
            # Align token expiry with kick timer: both count from invite delivery, not from
            # first DB insert (which can be seconds earlier and made the web UI expire first).
            deadline = utc_now() + timedelta(seconds=verify_timeout_seconds)
            vt_row.expires_at = deadline
    if active:
        schedule_verification_timeout(
            bot, chat_id=chat_id, user_id=user_id, token=token,
            delay=float(verify_timeout_seconds), kick=kick_on_verify_timeout,
        )
    else:
        await delete_stored_verify_invite(bot, invite_message_chat_id=invite_msg.chat.id, invite_message_id=invite_msg.message_id)

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


async def _retire_membership_verification(event: ChatMemberUpdated, bot: Bot, *, admin: bool, event_update: Update | None = None) -> None:
    user = event.new_chat_member.user
    if user.is_bot:
        return
    chat_id, user_id = event.chat.id, user.id
    event_at, update_id = getattr(event, "date", None), getattr(event_update, "update_id", None)
    running = _membership_versions.get((chat_id, user_id))
    if running is not None and _event_precedes(running.event_at, running.update_id, event_at, update_id):
        return
    for notice in _pending_join_events.get((chat_id, user_id), []):
        if _event_precedes(event_at, update_id, notice.event_at, notice.update_id):
            notice.cancelled = True
    # Cancel delayed work before waiting for its member lock; admin messages must not
    # be removed while a previous mute request is waiting out a server cooldown.
    set_member_verification_exempt(chat_id, user_id, admin)
    _membership_versions.pop((chat_id, user_id), None)
    retirement = _PendingJoin(0, event_at, update_id)
    _pending_membership_retirements[(chat_id, user_id)] = retirement
    try:
        invitations: list[tuple[int | None, int | None]] = []
        async with _member_lock(chat_id, user_id):
            async with session_scope() as session:
                await touch_known_chat(session, chat_id, event.chat.title)
                await get_or_create_group_settings(session, chat_id)
                tokens = (await session.scalars(select(VerifyToken).where(
                    VerifyToken.chat_id == chat_id, VerifyToken.user_id == user_id, VerifyToken.used_at.is_(None),
                ).with_for_update())).all()
                state = await session.get(UserChatState, (chat_id, user_id))
                if state is None:
                    state = UserChatState(chat_id=chat_id, user_id=user_id)
                    session.add(state)
                if _membership_event_stale(state, event_at, update_id):
                    set_member_verification_exempt(chat_id, user_id, state.membership_status == "admin")
                    return
                for token in tokens:
                    token.used_at = utc_now()
                    invitations.append((token.invite_message_chat_id, token.invite_message_id))
                state.membership_status = "admin" if admin else "left"
                state.membership_event_at = event_at
                state.membership_update_id = update_id
                state.membership_join_sources = 0
                state.verified = admin
                state.verified_at = utc_now() if admin else None
        for chat, message in invitations:
            await delete_stored_verify_invite(bot, invite_message_chat_id=chat, invite_message_id=message)
    finally:
        if _pending_membership_retirements.get((chat_id, user_id)) is retirement:
            _pending_membership_retirements.pop((chat_id, user_id), None)


@router.chat_member(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.new_chat_member.status.in_([ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]),
)
async def on_member_promoted(event: ChatMemberUpdated, bot: Bot, event_update: Update | None = None) -> None:
    await _retire_membership_verification(event, bot, admin=True, event_update=event_update)


@router.chat_member(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.new_chat_member.status.in_([ChatMemberStatus.LEFT, ChatMemberStatus.KICKED])
    | ((F.new_chat_member.status == ChatMemberStatus.RESTRICTED) & (F.new_chat_member.is_member == False)),  # noqa: E712
)
async def on_member_departed(event: ChatMemberUpdated, bot: Bot, event_update: Update | None = None) -> None:
    await _retire_membership_verification(event, bot, admin=False, event_update=event_update)


@router.chat_member(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.old_chat_member.status.in_([ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]),
    ~F.new_chat_member.status.in_([ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]),
)
async def on_member_demoted(event: ChatMemberUpdated, event_update: Update | None = None) -> None:
    chat_id, user_id = event.chat.id, event.new_chat_member.user.id
    event_at, update_id = getattr(event, "date", None), getattr(event_update, "update_id", None)
    async with _member_lock(chat_id, user_id):
        async with session_scope() as session:
            state = await session.get(UserChatState, (chat_id, user_id))
            if state is not None and not _membership_event_stale(state, event_at, update_id):
                state.membership_status = "joined"
                state.membership_update_id, state.membership_event_at = update_id, event_at
                state.membership_join_sources = 3
                set_member_verification_exempt(chat_id, user_id, False)


@router.chat_member(
    ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION),
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
)
async def on_user_joined(event: ChatMemberUpdated, bot: Bot, event_update: Update | None = None) -> None:
    user = event.new_chat_member.user
    if user.is_bot:
        return
    pending = _begin_pending_join(
        event.chat.id, user.id, source=1, event_at=getattr(event, "date", None),
        update_id=getattr(event_update, "update_id", None),
    )
    try:
        direct = _direct_admin_add_from_chat_member(event)
        if direct:
            direct = await _inviter_is_admin(bot, event.chat.id, event.from_user.id)
        await _start_verification_for_member(
            bot, chat_id=event.chat.id, user_id=user.id, chat_title=event.chat.title,
            user_language_code=user.language_code, message_thread_id=getattr(event, "message_thread_id", None),
            direct_admin_add=direct,
            membership_source=1, membership_event_at=getattr(event, "date", None),
            membership_update_id=getattr(event_update, "update_id", None),
        )
    finally:
        _end_pending_join(event.chat.id, user.id, pending)


@router.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.new_chat_members,
)
async def on_new_chat_members(message: Message, bot: Bot, event_update: Update | None = None) -> None:
    if not message.new_chat_members:
        return
    users = [user for user in message.new_chat_members if not user.is_bot]
    pending = [(user.id, _begin_pending_join(
        message.chat.id, user.id, source=2, event_at=getattr(message, "date", None),
        update_id=getattr(event_update, "update_id", None),
    )) for user in users]
    try:
        await _handle_new_chat_members(message, bot, event_update=event_update)
    finally:
        for user_id, notice in pending:
            _end_pending_join(message.chat.id, user_id, notice)


async def _handle_new_chat_members(message: Message, bot: Bot, event_update: Update | None = None) -> None:
    async with session_scope() as session:
        gs = await get_or_create_group_settings(session, message.chat.id)
        should_del_join = gs.delete_join_message
        verification_on = gs.verification_enabled

    has_human = any(not u.is_bot for u in message.new_chat_members)
    # Record the notice only if it will not be deleted after member handling.
    join_c = message.chat.id if verification_on and has_human and not should_del_join else None
    join_m = message.message_id if verification_on and has_human and not should_del_join else None
    for user in message.new_chat_members:
        if user.is_bot:
            continue
        direct = _direct_admin_add_from_new_members_message(message, user.id)
        if direct:
            direct = await _inviter_is_admin(bot, message.chat.id, message.from_user.id)
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
            membership_source=2, membership_event_at=getattr(message, "date", None),
            membership_update_id=getattr(event_update, "update_id", None),
        )

    # Deleting the service notice can wait until members have been muted.
    if should_del_join:
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except TelegramBadRequest as e:
            log.warning("delete join message failed: %s", e)
