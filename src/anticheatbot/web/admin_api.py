from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import web
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from sqlalchemy import delete, func, select

from anticheatbot.config import Settings
from anticheatbot.db.models import AuditEvent, GroupSettings, KnownChat, LLMTranslationCache, QuizQuestion
from anticheatbot.db.session import session_scope
from anticheatbot.init_data import InitDataInvalidError, parse_user_id, validate_init_data
from anticheatbot.services.groups import get_or_create_group_settings, touch_known_chat
from anticheatbot.web.json_body import json_object

log = logging.getLogger(__name__)


def _parse_init(request: web.Request, settings: Settings) -> dict[str, Any]:
    raw = request.headers.get("X-Telegram-Init-Data")
    if not raw:
        raise web.HTTPBadRequest(text="missing X-Telegram-Init-Data")
    try:
        return validate_init_data(
            raw,
            bot_token=settings.bot_token,
            max_age_seconds=settings.init_data_max_age_seconds,
        )
    except InitDataInvalidError as e:
        raise web.HTTPForbidden(text=str(e)) from e


def _is_global_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.global_admin_id_set


async def _is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
    except Exception as e:
        log.debug("get_chat_member failed: %s", e)
        return False
    return m.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)


async def _require_admin_access(request: web.Request, chat_id: int, user_id: int, settings: Settings) -> None:
    if _is_global_admin(user_id, settings):
        return
    bot: Bot = request.app["bot"]
    if not await _is_chat_admin(bot, chat_id, user_id):
        raise web.HTTPForbidden(text="not chat admin")


async def _group_settings_for_request(
    session,
    *,
    chat_id: int,
    user_id: int,
    settings: Settings,
) -> GroupSettings | None:
    gs = await session.get(GroupSettings, chat_id)
    if gs is not None:
        return gs
    if _is_global_admin(user_id, settings):
        await touch_known_chat(session, chat_id, None)
        return await get_or_create_group_settings(session, chat_id)
    return None


def _chat_id_param(request: web.Request) -> int:
    raw = request.rel_url.query.get("chat_id")
    if not raw:
        raise web.HTTPBadRequest(text="missing chat_id")
    try:
        return int(raw)
    except ValueError as e:
        raise web.HTTPBadRequest(text="bad chat_id") from e


async def admin_session(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    return web.json_response(
        {
            "user_id": user_id,
            "global_admin": _is_global_admin(user_id, settings),
        }
    )


async def admin_chats(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    bot: Bot = request.app["bot"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)

    async with session_scope() as session:
        stmt = select(KnownChat.chat_id, KnownChat.title)
        rows = (await session.execute(stmt)).all()

    if _is_global_admin(user_id, settings):
        out = [{"chat_id": cid, "title": title} for cid, title in rows]
        return web.json_response({"chats": out})

    out: list[dict[str, Any]] = []
    for cid, title in rows:
        if await _is_chat_admin(bot, cid, user_id):
            out.append({"chat_id": cid, "title": title})
    return web.json_response({"chats": out})


async def admin_settings_get(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    chat_id = _chat_id_param(request)
    await _require_admin_access(request, chat_id, user_id, settings)

    async with session_scope() as session:
        gs = await _group_settings_for_request(session, chat_id=chat_id, user_id=user_id, settings=settings)
        if gs is None:
            raise web.HTTPNotFound()
        data = {
            "chat_id": gs.chat_id,
            "verification_enabled": gs.verification_enabled,
            "verify_mode": gs.verify_mode,
            "verify_timeout_seconds": gs.verify_timeout_seconds,
            "kick_on_verify_timeout": gs.kick_on_verify_timeout,
            "turnstile_enabled": gs.turnstile_enabled,
            "rules_markdown": gs.rules_markdown,
            "canonical_locale": gs.canonical_locale,
            "llm_translation_enabled": gs.llm_translation_enabled,
            "translation_allowed_locales": gs.translation_allowed_locales,
            "quiz_pass_score_threshold": gs.quiz_pass_score_threshold,
            "quiz_draw_count": gs.quiz_draw_count,
            "llm_enabled": gs.llm_enabled,
            "llm_max_messages": gs.llm_max_messages,
            "llm_min_confidence_action": gs.llm_min_confidence_action,
            "spam_action": gs.spam_action,
        }
    return web.json_response(data)


async def admin_settings_patch(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    chat_id = _chat_id_param(request)
    await _require_admin_access(request, chat_id, user_id, settings)
    body = await json_object(request)

    allowed = {
        "verification_enabled",
        "verify_mode",
        "verify_timeout_seconds",
        "kick_on_verify_timeout",
        "turnstile_enabled",
        "rules_markdown",
        "canonical_locale",
        "llm_translation_enabled",
        "translation_allowed_locales",
        "quiz_pass_score_threshold",
        "quiz_draw_count",
        "llm_enabled",
        "llm_max_messages",
        "llm_min_confidence_action",
        "spam_action",
    }

    async with session_scope() as session:
        gs = await _group_settings_for_request(session, chat_id=chat_id, user_id=user_id, settings=settings)
        if gs is None:
            raise web.HTTPNotFound()
        for k, v in body.items():
            if k not in allowed:
                continue
            setattr(gs, k, v)
        await session.flush()
        session.add(
            AuditEvent(
                event_type="settings_changed",
                chat_id=chat_id,
                user_id=user_id,
                payload_json={"keys": list(body.keys()), "global_admin": _is_global_admin(user_id, settings)},
            )
        )
        await session.flush()

    return web.json_response({"ok": True})


async def admin_stats(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    chat_id = _chat_id_param(request)
    await _require_admin_access(request, chat_id, user_id, settings)
    raw_days = request.rel_url.query.get("range_days", "7")
    try:
        days = int(raw_days)
    except ValueError as e:
        raise web.HTTPBadRequest(text="bad range_days") from e
    since = datetime.now(tz=UTC) - timedelta(days=days)

    async with session_scope() as session:
        gs = await _group_settings_for_request(session, chat_id=chat_id, user_id=user_id, settings=settings)
        if gs is None:
            raise web.HTTPNotFound()
        stmt = (
            select(AuditEvent.event_type, func.count())
            .where(AuditEvent.chat_id == chat_id, AuditEvent.ts >= since)
            .group_by(AuditEvent.event_type)
        )
        rows = (await session.execute(stmt)).all()
    counts = {t: int(c) for t, c in rows}
    return web.json_response({"since": since.isoformat(), "counts": counts})


async def admin_translation_cache_clear(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    chat_id = _chat_id_param(request)
    await _require_admin_access(request, chat_id, user_id, settings)

    async with session_scope() as session:
        gs = await _group_settings_for_request(session, chat_id=chat_id, user_id=user_id, settings=settings)
        if gs is None:
            raise web.HTTPNotFound()
        await session.execute(delete(LLMTranslationCache).where(LLMTranslationCache.chat_id == chat_id))
        await session.flush()
    return web.json_response({"ok": True})


async def admin_quiz_list(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    chat_id = _chat_id_param(request)
    await _require_admin_access(request, chat_id, user_id, settings)

    async with session_scope() as session:
        gs = await _group_settings_for_request(session, chat_id=chat_id, user_id=user_id, settings=settings)
        if gs is None:
            raise web.HTTPNotFound()
        stmt = select(QuizQuestion).where(QuizQuestion.chat_id == chat_id).order_by(QuizQuestion.id)
        qs = list((await session.scalars(stmt)).all())
    out = [
        {
            "id": q.id,
            "prompt": q.prompt,
            "choices": q.choices_json,
            "correct_index": q.correct_index,
            "points": q.points,
        }
        for q in qs
    ]
    return web.json_response({"questions": out})


async def admin_quiz_upsert(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    chat_id = _chat_id_param(request)
    await _require_admin_access(request, chat_id, user_id, settings)
    body = await json_object(request)
    qid = body.get("id")
    prompt = body.get("prompt")
    choices = body.get("choices")
    correct_index = body.get("correct_index")
    points = body.get("points")
    if prompt is None or choices is None or correct_index is None or points is None:
        raise web.HTTPBadRequest(text="missing fields")
    if not isinstance(choices, list) or not all(isinstance(x, str) for x in choices):
        raise web.HTTPBadRequest(text="bad choices")
    try:
        ci = int(correct_index)
        pts = int(points)
    except (TypeError, ValueError) as e:
        raise web.HTTPBadRequest(text="bad correct_index or points") from e

    async with session_scope() as session:
        gs = await _group_settings_for_request(session, chat_id=chat_id, user_id=user_id, settings=settings)
        if gs is None:
            raise web.HTTPNotFound()
        if qid:
            try:
                qid_int = int(qid)
            except (TypeError, ValueError) as e:
                raise web.HTTPBadRequest(text="bad id") from e
            q = await session.get(QuizQuestion, qid_int)
            if q is None or q.chat_id != chat_id:
                raise web.HTTPNotFound()
            q.prompt = str(prompt)
            q.choices_json = choices
            q.correct_index = ci
            q.points = pts
        else:
            session.add(
                QuizQuestion(
                    chat_id=chat_id,
                    prompt=str(prompt),
                    choices_json=choices,
                    correct_index=ci,
                    points=pts,
                )
            )
        await session.flush()
    return web.json_response({"ok": True})


async def admin_quiz_delete(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    chat_id = _chat_id_param(request)
    await _require_admin_access(request, chat_id, user_id, settings)
    raw_qid = request.rel_url.query.get("id", "0")
    try:
        qid = int(raw_qid)
    except ValueError as e:
        raise web.HTTPBadRequest(text="bad id") from e
    if not qid:
        raise web.HTTPBadRequest(text="missing id")

    async with session_scope() as session:
        gs = await _group_settings_for_request(session, chat_id=chat_id, user_id=user_id, settings=settings)
        if gs is None:
            raise web.HTTPNotFound()
        q = await session.get(QuizQuestion, qid)
        if q is None or q.chat_id != chat_id:
            raise web.HTTPNotFound()
        await session.delete(q)
        await session.flush()
    return web.json_response({"ok": True})
