from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import web
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from sqlalchemy import delete, desc, func, literal, select

from anticheatbot.config import Settings
from anticheatbot.db.models import AuditEvent, GroupSettings, KnownChat, LLMTranslationCache, QuizQuestion
from anticheatbot.db.session import session_scope
from anticheatbot.init_data import InitDataInvalidError, parse_user_id, validate_init_data
from anticheatbot.locale_data import normalize_locale
from anticheatbot.services.groups import get_or_create_group_settings, touch_known_chat
from anticheatbot.web.json_body import json_object

log = logging.getLogger(__name__)

_VERIFY_MODES = frozenset({"rules_ack", "quiz"})
_SPAM_ACTIONS = frozenset({"delete", "delete_and_restrict"})
_MAX_RULES_MARKDOWN = 200_000
_MAX_WELCOME_MARKDOWN = 4000
_MAX_TRANSLATION_ALLOWED_RAW = 8192
_MAX_CANONICAL_LOCALE_LEN = 64
_MAX_QUIZ_PROMPT_LEN = 8000
_MAX_QUIZ_CHOICE_LEN = 2000
_MAX_QUIZ_CHOICES = 16
_MAX_QUIZ_BATCH = 120


def _strict_quiz_int(value: object, *, field: str, err_prefix: str = "") -> int:
    """Reject bool and non-integral floats so JSON like ``true`` / ``1.7`` cannot slip through."""
    if isinstance(value, bool):
        raise web.HTTPBadRequest(text=f"{err_prefix}bad {field}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise web.HTTPBadRequest(text=f"{err_prefix}bad {field}")
        return int(value)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise web.HTTPBadRequest(text=f"{err_prefix}bad {field}") from e


_ADMIN_BOOL_KEYS = frozenset(
    {
        "verification_enabled",
        "kick_on_verify_timeout",
        "turnstile_enabled",
        "delete_join_message",
        "block_guest_bots",
        "llm_translation_enabled",
        "llm_enabled",
    }
)


def _as_int_bounded(key: str, value: object, *, min_v: int, max_v: int) -> int:
    if isinstance(value, bool):
        raise web.HTTPBadRequest(text=f"invalid type for {key}")
    if isinstance(value, int):
        i = value
    elif isinstance(value, float):
        raise web.HTTPBadRequest(text=f"invalid type for {key}")
    elif isinstance(value, str):
        try:
            i = int(value.strip(), 10)
        except ValueError as e:
            raise web.HTTPBadRequest(text=f"invalid int for {key}") from e
    else:
        raise web.HTTPBadRequest(text=f"invalid type for {key}")
    if i < min_v or i > max_v:
        raise web.HTTPBadRequest(text=f"{key} out of range")
    return i


def _as_float_0_1(key: str, value: object) -> float:
    if isinstance(value, bool):
        raise web.HTTPBadRequest(text=f"invalid type for {key}")
    if isinstance(value, (int, float)):
        x = float(value)
    elif isinstance(value, str):
        try:
            x = float(value.strip().replace(",", "."))
        except ValueError as e:
            raise web.HTTPBadRequest(text=f"invalid float for {key}") from e
    else:
        raise web.HTTPBadRequest(text=f"invalid type for {key}")
    if x < 0.0 or x > 1.0:
        raise web.HTTPBadRequest(text=f"{key} out of range")
    return x


def _coerce_admin_settings_value(key: str, value: object) -> Any:
    """Normalize JSON patch values so ORM columns always get correct Python types (e.g. bool, not str)."""
    if key in _ADMIN_BOOL_KEYS:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(int(value))
        if value is None:
            return False
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("1", "true", "yes", "on"):
                return True
            if s in ("0", "false", "no", "off", ""):
                return False
        raise web.HTTPBadRequest(text=f"invalid boolean for {key}")
    if key == "post_verify_behavior":
        v = str(value).strip().lower() if value is not None else ""
        if v not in ("delete_only", "welcome"):
            raise web.HTTPBadRequest(text="bad post_verify_behavior")
        return v
    if key == "verify_mode":
        v = str(value or "").strip()
        if v not in _VERIFY_MODES:
            raise web.HTTPBadRequest(text="bad verify_mode")
        return v
    if key == "spam_action":
        v = str(value or "").strip()
        if v not in _SPAM_ACTIONS:
            raise web.HTTPBadRequest(text="bad spam_action")
        return v
    if key == "welcome_message_markdown":
        s = "" if value is None else str(value)
        if len(s) > _MAX_WELCOME_MARKDOWN:
            raise web.HTTPBadRequest(text="welcome_message_markdown too long")
        return s
    if key == "rules_markdown":
        s = "" if value is None else str(value)
        if len(s) > _MAX_RULES_MARKDOWN:
            raise web.HTTPBadRequest(text="rules_markdown too long")
        return s
    if key == "canonical_locale":
        if value is None:
            return "zh-Hans"
        s = str(value).strip()
        if not s:
            return "zh-Hans"
        if len(s) > _MAX_CANONICAL_LOCALE_LEN:
            raise web.HTTPBadRequest(text="canonical_locale too long")
        return normalize_locale(s)
    if key == "translation_allowed_locales":
        if value is None:
            return None
        s = str(value)
        if len(s) > _MAX_TRANSLATION_ALLOWED_RAW:
            raise web.HTTPBadRequest(text="translation_allowed_locales too long")
        return s
    if key == "verify_timeout_seconds":
        return _as_int_bounded(key, value, min_v=30, max_v=604_800)
    if key == "quiz_pass_min_correct":
        return _as_int_bounded(key, value, min_v=1, max_v=50)
    if key == "quiz_draw_count":
        return _as_int_bounded(key, value, min_v=1, max_v=50)
    if key == "llm_max_messages":
        return _as_int_bounded(key, value, min_v=0, max_v=50)
    if key == "llm_min_confidence_action":
        return _as_float_0_1(key, value)
    return value


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

    all_chats: list[dict[str, Any]] = [{"chat_id": int(cid), "title": title} for cid, title in rows]

    if _is_global_admin(user_id, settings):
        mine: list[dict[str, Any]] = []
        for c in all_chats:
            if await _is_chat_admin(bot, c["chat_id"], user_id):
                mine.append(c)
        return web.json_response({"chats_mine": mine, "chats_global": all_chats})

    out: list[dict[str, Any]] = []
    for c in all_chats:
        if await _is_chat_admin(bot, c["chat_id"], user_id):
            out.append(c)
    return web.json_response({"chats": out})


async def admin_settings_get(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    chat_id = _chat_id_param(request)
    await _require_admin_access(request, chat_id, user_id, settings)
    bot: Bot = request.app["bot"]

    async with session_scope() as session:
        gs = await _group_settings_for_request(session, chat_id=chat_id, user_id=user_id, settings=settings)
        if gs is None:
            raise web.HTTPNotFound()
        chat_title = await session.scalar(select(KnownChat.title).where(KnownChat.chat_id == chat_id))
        if chat_title is not None:
            chat_title = str(chat_title).strip() or None
        if not chat_title:
            try:
                tchat = await bot.get_chat(chat_id)
                t = (getattr(tchat, "title", None) or "").strip()
                if t:
                    await touch_known_chat(session, chat_id, t)
                    chat_title = t
            except Exception:
                log.debug("admin_settings_get: get_chat failed for chat_id=%s", chat_id, exc_info=True)
        data = {
            "chat_id": gs.chat_id,
            "chat_title": chat_title,
            "verification_enabled": gs.verification_enabled,
            "verify_mode": gs.verify_mode,
            "verify_timeout_seconds": gs.verify_timeout_seconds,
            "kick_on_verify_timeout": gs.kick_on_verify_timeout,
            "turnstile_enabled": gs.turnstile_enabled,
            "delete_join_message": gs.delete_join_message,
            "post_verify_behavior": gs.post_verify_behavior,
            "welcome_message_markdown": gs.welcome_message_markdown,
            "block_guest_bots": gs.block_guest_bots,
            "rules_markdown": gs.rules_markdown,
            "canonical_locale": gs.canonical_locale,
            "llm_translation_enabled": gs.llm_translation_enabled,
            "translation_allowed_locales": gs.translation_allowed_locales,
            "quiz_pass_min_correct": gs.quiz_pass_min_correct,
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
        "delete_join_message",
        "post_verify_behavior",
        "welcome_message_markdown",
        "block_guest_bots",
        "rules_markdown",
        "canonical_locale",
        "llm_translation_enabled",
        "translation_allowed_locales",
        "quiz_pass_min_correct",
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
            setattr(gs, k, _coerce_admin_settings_value(k, v))
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
    days = max(1, min(days, 366))
    raw_log_limit = request.rel_url.query.get("log_limit", "400")
    try:
        log_limit = int(raw_log_limit)
    except ValueError as e:
        raise web.HTTPBadRequest(text="bad log_limit") from e
    log_limit = max(1, min(log_limit, 500))
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

        bind = session.get_bind()
        dialect = (getattr(getattr(bind, "dialect", None), "name", None) or "sqlite").lower()
        if dialect == "sqlite":
            day_expr = func.strftime("%Y-%m-%d", AuditEvent.ts)
        else:
            day_expr = func.to_char(AuditEvent.ts, literal("YYYY-MM-DD"))
        stmt_daily = (
            select(day_expr, AuditEvent.event_type, func.count())
            .where(AuditEvent.chat_id == chat_id, AuditEvent.ts >= since)
            .group_by(day_expr, AuditEvent.event_type)
            .order_by(day_expr.asc())
        )
        daily_rows = (await session.execute(stmt_daily)).all()

        stmt_ev = (
            select(AuditEvent)
            .where(AuditEvent.chat_id == chat_id, AuditEvent.ts >= since)
            .order_by(desc(AuditEvent.ts))
            .limit(log_limit)
        )
        ev_list = list((await session.scalars(stmt_ev)).all())

    daily_map: dict[str, dict[str, int]] = {}
    for d, et, c in daily_rows:
        day_str = d.isoformat() if hasattr(d, "isoformat") and not isinstance(d, str) else str(d)
        if day_str not in daily_map:
            daily_map[day_str] = {}
        daily_map[day_str][et] = int(c)
    daily = [{"day": day, "counts": daily_map[day]} for day in sorted(daily_map.keys())]

    events_out: list[dict[str, Any]] = []
    for ev in ev_list:
        ts = ev.ts
        if ts.tzinfo is None:
            ts_iso = ts.replace(tzinfo=UTC).isoformat()
        else:
            ts_iso = ts.astimezone(UTC).isoformat()
        events_out.append(
            {
                "id": ev.id,
                "ts": ts_iso,
                "event_type": ev.event_type,
                "user_id": ev.user_id,
                "payload": ev.payload_json,
            }
        )

    total_events = int(sum(counts.values()))
    return web.json_response(
        {
            "since": since.isoformat(),
            "range_days": days,
            "counts": counts,
            "total_events": total_events,
            "daily": daily,
            "events": events_out,
            "log_limit": log_limit,
        }
    )


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
        }
        for q in qs
    ]
    return web.json_response({"questions": out})


def _coerce_quiz_question_dict(body: dict[str, Any], *, label: str = "") -> tuple[str, list[str], int]:
    """Validate one question payload. ``label`` prefixes error text (e.g. ``q[3]`` for batch import)."""
    prefix = f"{label}: " if label else ""
    prompt = body.get("prompt")
    choices = body.get("choices")
    correct_index = body.get("correct_index")
    if prompt is None or choices is None or correct_index is None:
        raise web.HTTPBadRequest(text=f"{prefix}missing fields")
    if not isinstance(choices, list) or not all(isinstance(x, str) for x in choices):
        raise web.HTTPBadRequest(text=f"{prefix}bad choices")
    if len(choices) < 2:
        raise web.HTTPBadRequest(text=f"{prefix}need at least 2 choices")
    if len(choices) > _MAX_QUIZ_CHOICES:
        raise web.HTTPBadRequest(text=f"{prefix}too many choices")
    choice_rows = [str(c).strip() for c in choices]
    if any(len(c) == 0 for c in choice_rows):
        raise web.HTTPBadRequest(text=f"{prefix}empty choice")
    for i, ch in enumerate(choice_rows):
        if len(ch) > _MAX_QUIZ_CHOICE_LEN:
            raise web.HTTPBadRequest(text=f"{prefix}choice {i} too long")
    ci = _strict_quiz_int(correct_index, field="correct_index", err_prefix=prefix)
    if ci < 0 or ci >= len(choice_rows):
        raise web.HTTPBadRequest(text=f"{prefix}correct_index out of range")
    prompt_str = str(prompt).strip()
    if not prompt_str:
        raise web.HTTPBadRequest(text=f"{prefix}empty prompt")
    if len(prompt_str) > _MAX_QUIZ_PROMPT_LEN:
        raise web.HTTPBadRequest(text=f"{prefix}prompt too long")
    return prompt_str, choice_rows, ci


async def admin_quiz_upsert(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    chat_id = _chat_id_param(request)
    await _require_admin_access(request, chat_id, user_id, settings)
    body = await json_object(request)
    qid = body.get("id")
    prompt_str, choice_rows, ci = _coerce_quiz_question_dict(body)

    async with session_scope() as session:
        gs = await _group_settings_for_request(session, chat_id=chat_id, user_id=user_id, settings=settings)
        if gs is None:
            raise web.HTTPNotFound()
        if qid not in (None, "", 0):
            if isinstance(qid, bool):
                raise web.HTTPBadRequest(text="bad id")
            try:
                qid_int = int(qid)
            except (TypeError, ValueError) as e:
                raise web.HTTPBadRequest(text="bad id") from e
            q = await session.get(QuizQuestion, qid_int)
            if q is None or q.chat_id != chat_id:
                raise web.HTTPNotFound()
            q.prompt = prompt_str
            q.choices_json = choice_rows
            q.correct_index = ci
            q.points = 1
        else:
            session.add(
                QuizQuestion(
                    chat_id=chat_id,
                    prompt=prompt_str,
                    choices_json=choice_rows,
                    correct_index=ci,
                    points=1,
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


async def admin_quiz_batch_import(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    chat_id = _chat_id_param(request)
    await _require_admin_access(request, chat_id, user_id, settings)
    body = await json_object(request)
    mode = body.get("mode", "append")
    if mode not in ("append", "replace"):
        raise web.HTTPBadRequest(text="bad mode")
    raw_questions = body.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise web.HTTPBadRequest(text="questions must be a non-empty array")
    if len(raw_questions) > _MAX_QUIZ_BATCH:
        raise web.HTTPBadRequest(text="too many questions")

    normalized: list[tuple[str, list[str], int]] = []
    for i, item in enumerate(raw_questions):
        if not isinstance(item, dict):
            raise web.HTTPBadRequest(text=f"q[{i}]: expected object")
        normalized.append(_coerce_quiz_question_dict(item, label=f"q[{i}]"))

    async with session_scope() as session:
        gs = await _group_settings_for_request(session, chat_id=chat_id, user_id=user_id, settings=settings)
        if gs is None:
            raise web.HTTPNotFound()
        if mode == "replace":
            await session.execute(delete(QuizQuestion).where(QuizQuestion.chat_id == chat_id))
        for prompt_str, choice_rows, ci in normalized:
            session.add(
                QuizQuestion(
                    chat_id=chat_id,
                    prompt=prompt_str,
                    choices_json=choice_rows,
                    correct_index=ci,
                    points=1,
                )
            )
        await session.flush()
    return web.json_response({"ok": True, "imported": len(normalized)})
