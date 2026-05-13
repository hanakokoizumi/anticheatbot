from __future__ import annotations

import asyncio
import json
import logging

from aiogram import Bot, Router, F
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import Message

from anticheatbot.config import get_settings
from anticheatbot.db.models import UserChatState
from anticheatbot.db.session import session_scope
from anticheatbot.llm import LLMError, moderate_message
from anticheatbot.services.audit import log_event
from anticheatbot.services.groups import get_or_create_group_settings

log = logging.getLogger(__name__)

router = Router(name="moderation")


def _text_preview(text: str, limit: int = 240) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


async def _run_moderation(
    *,
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    user_id: int,
    threshold: float,
    model: str,
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
        if ucs is None or not ucs.verified:
            return
        gs = await get_or_create_group_settings(session, chat_id)
        if not gs.llm_enabled:
            return
        if ucs.llm_messages_seen >= gs.llm_max_messages:
            return
        ucs.llm_messages_seen += 1
        threshold = gs.llm_min_confidence_action
        mod_model = settings.effective_moderation_model
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
        )
    )
