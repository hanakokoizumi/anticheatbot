from __future__ import annotations

import asyncio
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


async def _run_moderation(
    *,
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    user_id: int,
    threshold: float,
) -> None:
    settings = get_settings()
    if not settings.openai_api_key:
        return
    try:
        verdict = await moderate_message(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            model=settings.effective_moderation_model,
            text=text,
        )
    except LLMError as e:
        log.warning("moderation llm error: %s", e)
        return
    label = str(verdict.get("label", "ok"))
    conf = float(verdict.get("confidence", 0))
    if label == "ok" or conf < threshold:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as e:
        log.warning("delete moderated message failed: %s", e)
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
        await session.flush()

    asyncio.create_task(
        _run_moderation(
            bot=bot,
            chat_id=chat_id,
            message_id=message.message_id,
            text=text,
            user_id=uid,
            threshold=threshold,
        )
    )
