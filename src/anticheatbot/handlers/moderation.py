from __future__ import annotations

import asyncio
import json
import logging

from aiogram import Bot, Router, F
from aiogram.enums import ChatMemberStatus, ChatType, MessageEntityType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message

from anticheatbot.config import get_settings
from anticheatbot.db.models import UserChatState
from anticheatbot.db.session import session_scope
from anticheatbot.kick_registry import pop_matching_recent_bot_kick
from anticheatbot.llm import LLMError, moderate_message
from anticheatbot.services.audit import log_event
from anticheatbot.services.groups import get_or_create_group_settings
from anticheatbot.time_utils import utc_now

log = logging.getLogger(__name__)

router = Router(name="moderation")


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
    text = message.text or ""
    entities = message.entities
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
) -> None:
    settings = get_settings()
    if not settings.openai_api_key:
        return
    log.info(
        "llm_moderation_begin chat_id=%s message_id=%s user_id=%s model=%s threshold=%s "
        "text_len=%s preview=%s",
        chat_id,
        message_id,
        user_id,
        model,
        threshold,
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
        return
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
        return
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
        return
    log.info(
        "llm_moderation_delete_ok chat_id=%s message_id=%s user_id=%s label=%s confidence=%s",
        chat_id,
        message_id,
        user_id,
        label,
        conf,
    )
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
            },
        )


@router.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.text,
    ~F.text.startswith("/"),
)
async def on_group_text(message: Message, bot: Bot) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    uid = message.from_user.id
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if not text:
        return

    settings = get_settings()
    if uid in settings.global_admin_id_set:
        return

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
        if not gs.llm_enabled:
            return
        if ucs is None:
            ucs = UserChatState(
                chat_id=chat_id,
                user_id=uid,
                verified=True,
                verified_at=utc_now(),
                llm_messages_seen=0,
            )
            session.add(ucs)
        if ucs.llm_messages_seen >= gs.llm_max_messages:
            return
        ucs.llm_messages_seen += 1
        threshold = gs.llm_min_confidence_action
        mod_model = settings.effective_moderation_model
        mention_usernames = _mention_usernames_from_message(message)
        await session.flush()

    asyncio.create_task(
        _run_moderation(
            bot=bot,
            chat_id=chat_id,
            message_id=message.message_id,
            text=text,
            user_id=uid,
            threshold=threshold,
            model=mod_model,
            mention_usernames=mention_usernames if mention_usernames else None,
        )
    )


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
