from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any
from weakref import WeakValueDictionary

from aiogram import Bot, Router, F
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ChatMemberStatus, ChatType, MessageEntityType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message

from anticheatbot.config import get_settings
from anticheatbot.db.models import GroupSettings, UserChatState
from anticheatbot.db.session import session_scope
from anticheatbot.handlers.verify_flow import member_lifecycle_is_current
from anticheatbot.kick_registry import pop_matching_recent_bot_kick
from anticheatbot.llm import LLMError, moderate_message
from anticheatbot.services.audit import log_event
from anticheatbot.services.groups import get_or_create_group_settings
from anticheatbot.telegram_perms import muted_member_permissions
from anticheatbot.time_utils import coerce_utc, utc_now

log = logging.getLogger(__name__)

router = Router(name="moderation")
_moderation_locks: WeakValueDictionary[tuple[int, int], asyncio.Lock] = WeakValueDictionary()
_moderation_semaphore = asyncio.Semaphore(8)


class _MessageVersion:
    blocked = False
    original_seen = False
    original_epoch: str | None = ""


_message_versions: WeakValueDictionary[tuple[int, int], _MessageVersion] = WeakValueDictionary()


def _review_window(state: UserChatState) -> tuple[str | None, dict[str, int]]:
    epoch = coerce_utc(state.verified_at).isoformat() if state.verified_at is not None else None
    saved = state.llm_review_state or {}
    if saved.get("verification_epoch") != epoch:
        return epoch, {}
    return epoch, dict(saved.get("messages", {}))


def _save_review_window(state: UserChatState, epoch: str | None, messages: dict[str, int]) -> None:
    state.llm_review_state = {"verification_epoch": epoch, "messages": messages}


def _text_preview(text: str, limit: int = 240) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def _utf16_slice(text: str, offset: int, length: int) -> str:
    """Slice ``text`` using Telegram UTF-16 code-unit offsets (``MessageEntity``)."""
    raw = text.encode("utf-16-le")
    b0, b1 = offset * 2, (offset + length) * 2
    return raw[b0:b1].decode("utf-16-le")


def _mention_usernames_from_message(message: Message) -> list[str]:
    """@handles from entities (``MENTION`` / ``TEXT_MENTION``), lowercased, de-duplicated."""
    text = message.text or message.caption or ""
    entities = message.entities if message.text else message.caption_entities
    if not text or not entities:
        return []
    names: list[str] = []
    for ent in entities:
        try:
            if ent.type == MessageEntityType.MENTION:
                seg = _utf16_slice(text, ent.offset, ent.length).lstrip("@").strip()
                if seg:
                    names.append(seg.lower())
            elif ent.type == MessageEntityType.TEXT_MENTION and ent.user and ent.user.username:
                names.append(ent.user.username.strip().lower())
        except (ValueError, UnicodeDecodeError, IndexError):
            continue
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


async def _current_moderation_context(
    chat_id: int, user_id: int, verification_epoch: datetime | None,
) -> tuple[GroupSettings, UserChatState] | None:
    async with session_scope() as session:
        gs = await session.get(GroupSettings, chat_id)
        current = await session.get(UserChatState, (chat_id, user_id))
        same_epoch = current is not None and (
            current.verified_at == verification_epoch
            or (current.verified_at is not None and verification_epoch is not None
                and coerce_utc(current.verified_at) == coerce_utc(verification_epoch))
        )
        if gs is None or not gs.llm_enabled or gs.llm_max_messages <= 0:
            return None
        if current is None or not current.verified or not same_epoch:
            return None
        if not member_lifecycle_is_current(chat_id, user_id, current, legacy_started_at=verification_epoch):
            return None
        return gs, current


async def _run_moderation(
    *,
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    user_id: int,
    threshold: float,
    model: str,
    mention_usernames: list[str] | None = None,
    version: _MessageVersion | None = None,
    verification_epoch: datetime | None = None,
    is_edit: bool = False,
) -> bool:
    settings = get_settings()
    if not settings.openai_api_key:
        return False
    log.info(
        "llm_moderation_begin chat_id=%s message_id=%s user_id=%s model=%s threshold=%s "
        "is_edit=%s text_len=%s preview=%s",
        chat_id,
        message_id,
        user_id,
        model,
        threshold,
        is_edit,
        len(text),
        _text_preview(text),
    )
    try:
        verdict = await moderate_message(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            model=model,
            text=text,
            mention_usernames=mention_usernames,
        )
    except LLMError as e:
        log.warning(
            "llm_moderation_failed chat_id=%s message_id=%s user_id=%s model=%s err=%s",
            chat_id,
            message_id,
            user_id,
            model,
            e,
        )
        return False
    if version is not None and _message_versions.get((chat_id, message_id)) is not version:
        # An edit arrived while this request was in flight. Review the current text.
        return False
    label = str(verdict.get("label", "ok"))
    try:
        conf = float(verdict.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    raw_verdict = json.dumps(verdict, ensure_ascii=False, default=str)
    log.info(
        "llm_moderation_verdict chat_id=%s message_id=%s user_id=%s label=%s confidence=%s "
        "reason=%r extras=%s",
        chat_id,
        message_id,
        user_id,
        label,
        conf,
        verdict.get("reason"),
        raw_verdict,
    )
    # Settings and membership can change while the model is thinking.
    context = await _current_moderation_context(chat_id, user_id, verification_epoch)
    if context is None:
        return False
    gs, current = context
    threshold = gs.llm_min_confidence_action
    spam_action = gs.spam_action
    if label == "ok" or conf < threshold:
        log.info(
            "llm_moderation_skip_action chat_id=%s message_id=%s user_id=%s label=%s confidence=%s "
            "threshold=%s (ok_or_below_threshold)",
            chat_id,
            message_id,
            user_id,
            label,
            conf,
            threshold,
        )
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError as e:
        log.warning("cannot confirm moderation permissions chat=%s user=%s: %s", chat_id, user_id, e)
        return False
    if member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
        return True
    context = await _current_moderation_context(chat_id, user_id, verification_epoch)
    if context is None:
        return False
    gs, current = context
    if conf < gs.llm_min_confidence_action or not member_lifecycle_is_current(
        chat_id, user_id, current, legacy_started_at=verification_epoch,
    ):
        return False
    spam_action = gs.spam_action
    if version is not None and _message_versions.get((chat_id, message_id)) is not version:
        return False
    if version is not None:
        # A rejected command must not trigger a reply, even if Telegram cannot delete it.
        version.blocked = True
    log.info(
        "llm_moderation_delete_attempt chat_id=%s message_id=%s user_id=%s label=%s confidence=%s",
        chat_id,
        message_id,
        user_id,
        label,
        conf,
    )
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as e:
        log.warning(
            "llm_moderation_delete_failed chat_id=%s message_id=%s user_id=%s err=%s",
            chat_id,
            message_id,
            user_id,
            e,
        )
        return False
    log.info(
        "llm_moderation_delete_ok chat_id=%s message_id=%s user_id=%s label=%s confidence=%s",
        chat_id,
        message_id,
        user_id,
        label,
        conf,
    )
    restricted = False
    # Deleting a message is another external wait; never apply its old verdict to
    # someone who has since rejoined or whose moderation policy was changed.
    context = await _current_moderation_context(chat_id, user_id, verification_epoch)
    if context is None:
        return False
    gs, current = context
    if conf < gs.llm_min_confidence_action or not member_lifecycle_is_current(
        chat_id, user_id, current, legacy_started_at=verification_epoch,
    ) or (version is not None and _message_versions.get((chat_id, message_id)) is not version):
        return False
    spam_action = gs.spam_action
    if spam_action == "delete_and_restrict" and member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        if member.status == ChatMemberStatus.MEMBER or member.is_member:
            try:
                await bot.restrict_chat_member(
                    chat_id, user_id, permissions=muted_member_permissions(), use_independent_chat_permissions=True,
                )
                restricted = True
            except TelegramAPIError as e:
                log.warning("moderation restriction failed chat=%s user=%s: %s", chat_id, user_id, e)
    async with session_scope() as session:
        await log_event(
            session,
            event_type="llm_action_delete",
            chat_id=chat_id,
            user_id=user_id,
            payload={
                "label": label,
                "confidence": conf,
                "reason": verdict.get("reason"),
                "action": spam_action,
                "restricted": restricted,
            },
        )
    return True


@router.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.text | F.caption,
)
async def on_group_text(message: Message, bot: Bot) -> Any:
    if not await _review_group_message(message, bot, is_edit=False):
        # Continue to /start and /admin only after the same quota-governed review.
        return UNHANDLED


@router.edited_message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
)
async def on_edited_group_text(message: Message, bot: Bot) -> None:
    await _review_group_message(message, bot, is_edit=True)


async def _review_group_message(message: Message, bot: Bot, *, is_edit: bool) -> bool:
    if message.from_user is None or message.from_user.is_bot:
        return False
    uid = message.from_user.id
    chat_id = message.chat.id
    settings = get_settings()
    if uid in settings.global_admin_id_set or not settings.openai_api_key:
        return False

    key = (chat_id, message.message_id)
    previous = _message_versions.get(key)
    version = _MessageVersion()
    # An edit can replace a new message before that message obtains the user lock.
    version.original_seen = not is_edit or (previous is not None and previous.original_seen)
    if is_edit and previous is not None:
        version.original_epoch = previous.original_epoch
    _message_versions[key] = version
    text = (message.text or message.caption or "").strip()
    if not text:
        return False
    lock = _moderation_locks.setdefault((chat_id, uid), asyncio.Lock())
    async with lock:
        if _message_versions.get(key) is not version:
            return True
        await _review_current_message(
            message, bot, is_edit=is_edit, version=version, text=text
        )
        return version.blocked or _message_versions.get(key) is not version


async def _review_current_message(
    message: Message, bot: Bot, *, is_edit: bool, version: _MessageVersion, text: str
) -> None:
    settings = get_settings()
    uid, chat_id = message.from_user.id, message.chat.id

    try:
        m = await bot.get_chat_member(chat_id, uid)
        if m.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
            return
    except Exception:
        return

    async with session_scope() as session:
        ucs = await session.get(UserChatState, (chat_id, uid))
        if ucs is not None and not ucs.verified:
            return
        gs = await get_or_create_group_settings(session, chat_id)
        if not gs.llm_enabled or gs.llm_max_messages <= 0:
            return
        if ucs is None:
            if is_edit and not version.original_seen:
                return
            ucs = UserChatState(
                chat_id=chat_id,
                user_id=uid,
                verified=True,
                verified_at=utc_now(),
                llm_messages_seen=0,
            )
            session.add(ucs)
        epoch, messages = _review_window(ucs)
        message_key = str(message.message_id)
        if is_edit:
            if message_key not in messages and (
                not version.original_seen or version.original_epoch not in ("", epoch)
            ):
                return
            if messages.get(message_key, 0) > gs.llm_max_messages:
                return
        if not is_edit or not messages.get(message_key, 0):
            if ucs.llm_messages_seen >= gs.llm_max_messages:
                return
        version.original_epoch = epoch
        if message_key not in messages:
            # Reserve eligibility before the model call so an in-flight edit can
            # replace its verdict. Only a completed review consumes a slot.
            messages[message_key] = 0
            _save_review_window(ucs, epoch, messages)
        verification_epoch = ucs.verified_at
        threshold = gs.llm_min_confidence_action
        mod_model = settings.effective_moderation_model
        mention_usernames = _mention_usernames_from_message(message)
        await session.flush()

    async with _moderation_semaphore:
        if _message_versions.get((chat_id, message.message_id)) is not version:
            return
        reviewed = await _run_moderation(
            bot=bot,
            chat_id=chat_id,
            message_id=message.message_id,
            text=text,
            user_id=uid,
            threshold=threshold,
            model=mod_model,
            mention_usernames=mention_usernames if mention_usernames else None,
            version=version,
            verification_epoch=verification_epoch,
            is_edit=is_edit,
        )
    async with session_scope() as session:
        current = await session.get(UserChatState, (chat_id, uid), with_for_update=True)
        same_epoch = current is not None and (
            current.verified_at == verification_epoch
            or (current.verified_at is not None and verification_epoch is not None
                and coerce_utc(current.verified_at) == coerce_utc(verification_epoch))
        )
        if current is not None and current.verified and same_epoch:
            epoch, messages = _review_window(current)
            if message_key not in messages or messages[message_key]:
                return
            if reviewed:
                current.llm_messages_seen += 1
                messages[message_key] = current.llm_messages_seen
            elif _message_versions.get((chat_id, message.message_id)) is version:
                # A failed request leaves the slot available. Keep the reservation
                # when a newer edit is queued and still needs this eligibility.
                del messages[message_key]
            _save_review_window(current, epoch, messages)


@router.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.left_chat_member,
)
async def on_bot_kick_service_message(message: Message, bot: Bot) -> None:
    """Delete Telegram service message after our ban/kick (register_recent_bot_kick)."""
    left = message.left_chat_member
    if left is None or left.is_bot:
        return
    if not await pop_matching_recent_bot_kick(chat_id=message.chat.id, kicked_user_id=left.id):
        return
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.warning(
            "delete kick service message failed chat_id=%s message_id=%s err=%s",
            message.chat.id,
            message.message_id,
            e,
        )
