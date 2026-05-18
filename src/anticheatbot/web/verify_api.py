from __future__ import annotations

import ipaddress
import json
import logging
import re
from datetime import datetime
from typing import Any

from aiohttp import web
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import update

from sqlalchemy.ext.asyncio import AsyncSession

from anticheatbot.config import Settings
from anticheatbot.db.models import GroupSettings, QuizQuestion, UserChatState, VerifyQuizSession, VerifyToken
from anticheatbot.db.session import session_scope
from anticheatbot.bot_i18n import bot_t, resolve_bot_locale
from anticheatbot.handlers.verify_flow import (
    delete_stored_verify_invite,
    delete_stored_verify_join_message,
    post_verify_behavior_normalized,
    send_post_verify_welcome,
)
from anticheatbot.init_data import (
    InitDataInvalidError,
    parse_language_code,
    parse_user_id,
    validate_init_data,
)
from anticheatbot.kick_registry import register_recent_bot_kick
from anticheatbot.locale_data import normalize_locale
from anticheatbot.services.audit import log_event
from anticheatbot.services.groups import get_or_create_group_settings
from anticheatbot.services.quiz import QuizError, draw_quiz_for_token, grade_answers, normalize_shuffle_order
from anticheatbot.telegram_perms import normal_member_permissions
from anticheatbot.time_utils import coerce_utc, utc_now
from anticheatbot.translation_service import translate_or_same
from anticheatbot.turnstile import verify_turnstile
from anticheatbot.web.json_body import json_object

log = logging.getLogger(__name__)

# DB column verify_tokens.token is VARCHAR(64); minted with secrets.token_urlsafe(24) (~32 chars).
_VERIFY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_MAX_QUIZ_ANSWER_KEYS = 128
_MAX_LANG_QUERY_LEN = 64


def _strict_answer_index(value: object) -> int:
    """JSON numbers may arrive as float; reject ``1.3`` so answers cannot be coerced to wrong indices."""
    if isinstance(value, bool):
        raise TypeError("bool answer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise TypeError("non-integer answer")
        return int(value)
    raise TypeError("bad answer type")


def _turnstile_client_ip(request: web.Request, settings: Settings) -> str | None:
    """Turnstile ``remoteip``: optional X-Forwarded-For only when explicitly trusted (spoofing risk)."""
    if settings.trust_x_forwarded_for:
        raw = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if raw and len(raw) <= 64:
            host = raw.partition("%")[0]
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                return raw
    r = (request.remote or "").strip()
    return r or None


def _parse_verify_token_query(raw: str | None) -> str:
    if raw is None or not str(raw).strip():
        raise web.HTTPBadRequest(text="missing t")
    t = str(raw).strip()
    if not _VERIFY_TOKEN_RE.fullmatch(t):
        raise web.HTTPForbidden(text="bad token")
    return t


def _parse_verify_token_body(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise web.HTTPBadRequest(text="missing token")
    t = value.strip()
    if not _VERIFY_TOKEN_RE.fullmatch(t):
        raise web.HTTPForbidden(text="bad token")
    return t


def _raise_wrong_verify_subject() -> None:
    """Opened by a Telegram user other than the member who owns this token."""
    raise web.HTTPForbidden(
        text=json.dumps({"error": "wrong_verify_subject"}),
        content_type="application/json",
    )


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


def _target_locale(request: web.Request, init_fields: dict[str, Any]) -> str:
    q = request.rel_url.query.get("lang")
    if q:
        q = str(q).strip()
        if len(q) > _MAX_LANG_QUERY_LEN:
            q = q[:_MAX_LANG_QUERY_LEN]
        return normalize_locale(q)
    return normalize_locale(parse_language_code(init_fields))


def _welcome_template_for_user(gs: GroupSettings, init_fields: dict[str, Any]) -> str:
    """Custom welcome body, or localized default from Telegram user.language_code in initData."""
    custom = (gs.welcome_message_markdown or "").strip()
    if custom:
        return custom
    loc = resolve_bot_locale(parse_language_code(init_fields))
    return bot_t(loc, "default_welcome")


async def verify_session(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    token_str = _parse_verify_token_query(request.rel_url.query.get("t"))

    async with session_scope() as session:
        vt = await session.get(VerifyToken, token_str)
        if vt is None:
            log.warning("verify_session: unknown token prefix=%s", token_str[:16] if token_str else "")
            raise web.HTTPForbidden(text="bad token")
        if vt.user_id != user_id:
            log.warning(
                "verify_session: user mismatch for token token_user_id=%s init_user_id=%s",
                vt.user_id,
                user_id,
            )
            _raise_wrong_verify_subject()
        if vt.used_at is not None:
            raise web.HTTPForbidden(text="token used")
        if coerce_utc(vt.expires_at) < utc_now():
            raise web.HTTPForbidden(text="token expired")

        gs = await get_or_create_group_settings(session, vt.chat_id)
        target = _target_locale(request, init_fields)

        rules_text, rules_hit = await translate_or_same(
            session,
            settings,
            gs,
            namespace="rules",
            entity_id="rules",
            chat_id=vt.chat_id,
            text=gs.rules_markdown,
            target_locale=target,
        )

        payload: dict[str, Any] = {
            "verify_mode": gs.verify_mode,
            "rules_markdown": rules_text,
            "turnstile_site_key": settings.turnstile_site_key if gs.turnstile_enabled else None,
            "turnstile_enabled": gs.turnstile_enabled,
            "resolved_locale": target,
            "translation_cache_hit_rules": rules_hit,
            "quiz_pass_min_correct": gs.quiz_pass_min_correct,
            "quiz_draw_count": gs.quiz_draw_count,
        }

        if gs.verify_mode == "quiz":
            existing = await session.get(VerifyQuizSession, token_str)
            shuffle_maps: dict[str, list[int]] = {}
            if existing is None:
                try:
                    drawn = await draw_quiz_for_token(session, chat_id=vt.chat_id, token=token_str, gs=gs)
                except QuizError as e:
                    raise web.HTTPBadRequest(text=str(e)) from e
                questions = drawn.questions
                shuffle_maps = drawn.shuffle_maps
            else:
                shuffle_maps = dict(existing.shuffle_maps_json or {})
                qids = existing.question_ids_json
                questions = []
                for qid in qids:
                    q = await session.get(QuizQuestion, qid)
                    if q:
                        questions.append(q)
            out_quiz = []
            hits: list[bool] = []
            for q in questions:
                tr_prompt, hit_p = await translate_or_same(
                    session,
                    settings,
                    gs,
                    namespace="quiz_prompt",
                    entity_id=str(q.id),
                    chat_id=vt.chat_id,
                    text=q.prompt,
                    target_locale=target,
                )
                choices_orig: list[str] = []
                ch_hit_row: list[bool] = []
                for i, ch in enumerate(q.choices_json):
                    tr_ch, hit_c = await translate_or_same(
                        session,
                        settings,
                        gs,
                        namespace="quiz_choice",
                        entity_id=f"{q.id}:{i}",
                        chat_id=vt.chat_id,
                        text=ch,
                        target_locale=target,
                    )
                    choices_orig.append(tr_ch)
                    ch_hit_row.append(hit_c)
                order = normalize_shuffle_order(shuffle_maps, int(q.id), len(choices_orig))
                choices_out = [choices_orig[order[i]] for i in range(len(order))]
                hits.append(hit_p and all(ch_hit_row))
                out_quiz.append({"id": q.id, "prompt": tr_prompt, "choices": choices_out})
            payload["quiz"] = out_quiz
            payload["translation_cache_hit_quiz"] = all(hits) if hits else True

        resp = web.json_response(payload)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        resp.headers["Pragma"] = "no-cache"
        return resp


async def _turnstile_or_403(settings: Settings, gs: GroupSettings, token: object, remote_ip: str | None) -> None:
    if not gs.turnstile_enabled:
        return
    secret = settings.turnstile_secret_key
    if not secret:
        raise web.HTTPInternalServerError(text="turnstile enabled but TURNSTILE_SECRET_KEY missing")
    tok: str | None
    if token is None:
        tok = None
    elif isinstance(token, str):
        tok = token.strip() or None
        if tok and len(tok) > 4096:
            raise web.HTTPForbidden(text="turnstile failed")
    else:
        tok = None
    if not tok or not await verify_turnstile(secret=secret, response_token=tok, remote_ip=remote_ip):
        raise web.HTTPForbidden(text="turnstile failed")


async def _consume_verify_token_once(session: AsyncSession, *, token_str: str) -> datetime:
    """Single-use token: atomically set ``used_at`` so double-submit / parallel requests only succeed once."""
    now = utc_now()
    res = await session.execute(
        update(VerifyToken)
        .where(VerifyToken.token == token_str, VerifyToken.used_at.is_(None))
        .values(used_at=now)
    )
    if res.rowcount != 1:
        raise web.HTTPForbidden(text="token used")
    return now


async def verify_rules_complete(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    bot: Bot = request.app["bot"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    body = await json_object(request)
    token_str = _parse_verify_token_body(body.get("token"))
    turnstile_tok = body.get("turnstile_token")
    remote_ip = _turnstile_client_ip(request, settings)

    invite_chat_id: int | None = None
    invite_message_id: int | None = None
    join_chat_id: int | None = None
    join_message_id: int | None = None
    post_verify_behavior = "delete_only"
    welcome_template = ""
    welcome_chat_id = 0
    welcome_user_id = 0
    async with session_scope() as session:
        vt = await session.get(VerifyToken, token_str)
        if vt is None:
            log.warning("verify_rules_complete: unknown token prefix=%s", token_str[:16] if token_str else "")
            raise web.HTTPForbidden(text="bad token")
        if vt.user_id != user_id:
            log.warning(
                "verify_rules_complete: user mismatch token_user_id=%s init_user_id=%s",
                vt.user_id,
                user_id,
            )
            _raise_wrong_verify_subject()
        if vt.used_at is not None:
            raise web.HTTPForbidden(text="token used")
        if coerce_utc(vt.expires_at) < utc_now():
            raise web.HTTPForbidden(text="token expired")

        gs = await get_or_create_group_settings(session, vt.chat_id)
        if gs.verify_mode != "rules_ack":
            raise web.HTTPBadRequest(text="wrong verify_mode")
        await _turnstile_or_403(settings, gs, turnstile_tok, remote_ip)

        post_verify_behavior = post_verify_behavior_normalized(gs.post_verify_behavior)
        welcome_template = _welcome_template_for_user(gs, init_fields)
        welcome_chat_id = vt.chat_id
        welcome_user_id = vt.user_id

        invite_chat_id = vt.invite_message_chat_id
        invite_message_id = vt.invite_message_id
        join_chat_id = vt.join_message_chat_id
        join_message_id = vt.join_message_id

        now_ts = await _consume_verify_token_once(session, token_str=token_str)
        vt.used_at = now_ts
        ucs = await session.get(UserChatState, (vt.chat_id, vt.user_id))
        if ucs is None:
            ucs = UserChatState(chat_id=vt.chat_id, user_id=vt.user_id, verified=True, verified_at=now_ts)
            session.add(ucs)
        else:
            ucs.verified = True
            ucs.verified_at = now_ts
        await session.flush()

        try:
            await bot.restrict_chat_member(
                vt.chat_id,
                vt.user_id,
                permissions=normal_member_permissions(),
                use_independent_chat_permissions=True,
            )
        except TelegramBadRequest as e:
            log.warning("unrestrict failed: %s", e)

        await log_event(
            session,
            event_type="verify_rules_ok",
            chat_id=vt.chat_id,
            user_id=vt.user_id,
            payload={"token": token_str[:8]},
        )

    await delete_stored_verify_invite(
        bot, invite_message_chat_id=invite_chat_id, invite_message_id=invite_message_id
    )
    await delete_stored_verify_join_message(
        bot, join_message_chat_id=join_chat_id, join_message_id=join_message_id
    )
    if post_verify_behavior == "welcome":
        await send_post_verify_welcome(
            bot,
            chat_id=welcome_chat_id,
            user_id=welcome_user_id,
            template=welcome_template,
        )
    return web.json_response({"ok": True})


async def verify_quiz_submit(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    bot: Bot = request.app["bot"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    body = await json_object(request)
    token_str = _parse_verify_token_body(body.get("token"))
    answers_raw = body.get("answers")
    if not isinstance(answers_raw, dict):
        raise web.HTTPBadRequest(text="missing token or answers")
    if len(answers_raw) > _MAX_QUIZ_ANSWER_KEYS:
        raise web.HTTPBadRequest(text="invalid answers")
    answers: dict[int, int] = {}
    try:
        for k, v in answers_raw.items():
            if isinstance(k, bool) or isinstance(v, bool):
                raise TypeError("bool in answers")
            answers[int(k)] = _strict_answer_index(v)
    except (TypeError, ValueError) as e:
        raise web.HTTPBadRequest(text="invalid answers") from e
    turnstile_tok = body.get("turnstile_token")
    remote_ip = _turnstile_client_ip(request, settings)

    invite_chat_id: int | None = None
    invite_message_id: int | None = None
    join_chat_id: int | None = None
    join_message_id: int | None = None
    post_verify_behavior = "delete_only"
    welcome_template = ""
    welcome_chat_id = 0
    welcome_user_id = 0
    correct_n = 0
    total_n = 0
    quiz_failed = False
    quiz_fail_payload: dict[str, Any] | None = None
    kick_chat_id = 0
    kick_user_id = 0
    async with session_scope() as session:
        vt = await session.get(VerifyToken, token_str)
        if vt is None:
            log.warning("verify_quiz_submit: unknown token prefix=%s", token_str[:16] if token_str else "")
            raise web.HTTPForbidden(text="bad token")
        if vt.user_id != user_id:
            log.warning(
                "verify_quiz_submit: user mismatch token_user_id=%s init_user_id=%s",
                vt.user_id,
                user_id,
            )
            _raise_wrong_verify_subject()
        if vt.used_at is not None:
            raise web.HTTPForbidden(text="token used")
        if coerce_utc(vt.expires_at) < utc_now():
            raise web.HTTPForbidden(text="token expired")

        gs = await get_or_create_group_settings(session, vt.chat_id)
        if gs.verify_mode != "quiz":
            raise web.HTTPBadRequest(text="wrong verify_mode")
        await _turnstile_or_403(settings, gs, turnstile_tok, remote_ip)

        try:
            correct_n, total_n = await grade_answers(session, token=token_str, answers=answers)
        except QuizError as e:
            raise web.HTTPBadRequest(text=str(e)) from e

        need = min(max(1, gs.quiz_pass_min_correct), max(1, total_n))
        if correct_n < need:
            quiz_failed = True
            quiz_fail_payload = {
                "ok": False,
                "correct": correct_n,
                "total": total_n,
                "need": need,
            }
            invite_chat_id = vt.invite_message_chat_id
            invite_message_id = vt.invite_message_id
            join_chat_id = vt.join_message_chat_id
            join_message_id = vt.join_message_id
            kick_chat_id = vt.chat_id
            kick_user_id = vt.user_id
            qs_fail = await session.get(VerifyQuizSession, token_str)
            if qs_fail is not None:
                await session.delete(qs_fail)
            vt.used_at = utc_now()
            await log_event(
                session,
                event_type="verify_quiz_fail",
                chat_id=vt.chat_id,
                user_id=vt.user_id,
                payload={"correct": correct_n, "total": total_n, "need": need},
            )
            await session.flush()
        else:
            invite_chat_id = vt.invite_message_chat_id
            invite_message_id = vt.invite_message_id
            join_chat_id = vt.join_message_chat_id
            join_message_id = vt.join_message_id
            post_verify_behavior = post_verify_behavior_normalized(gs.post_verify_behavior)
            welcome_template = _welcome_template_for_user(gs, init_fields)
            welcome_chat_id = vt.chat_id
            welcome_user_id = vt.user_id

            now_ts = await _consume_verify_token_once(session, token_str=token_str)
            vt.used_at = now_ts
            ucs = await session.get(UserChatState, (vt.chat_id, vt.user_id))
            if ucs is None:
                ucs = UserChatState(
                    chat_id=vt.chat_id, user_id=vt.user_id, verified=True, verified_at=now_ts
                )
                session.add(ucs)
            else:
                ucs.verified = True
                ucs.verified_at = now_ts
            qs = await session.get(VerifyQuizSession, token_str)
            if qs is not None:
                await session.delete(qs)
            await session.flush()

            try:
                await bot.restrict_chat_member(
                    vt.chat_id,
                    vt.user_id,
                    permissions=normal_member_permissions(),
                    use_independent_chat_permissions=True,
                )
            except TelegramBadRequest as e:
                log.warning("unrestrict failed: %s", e)

            await log_event(
                session,
                event_type="verify_quiz_ok",
                chat_id=vt.chat_id,
                user_id=vt.user_id,
                payload={"correct": correct_n, "total": total_n},
            )

    if quiz_failed and quiz_fail_payload is not None:
        await delete_stored_verify_invite(
            bot, invite_message_chat_id=invite_chat_id, invite_message_id=invite_message_id
        )
        await delete_stored_verify_join_message(
            bot, join_message_chat_id=join_chat_id, join_message_id=join_message_id
        )
        try:
            await register_recent_bot_kick(chat_id=kick_chat_id, user_id=kick_user_id)
            await bot.ban_chat_member(kick_chat_id, kick_user_id)
            await bot.unban_chat_member(kick_chat_id, kick_user_id)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            log.warning("kick on verify quiz fail failed: %s", e)
        resp = web.json_response(quiz_fail_payload, status=403)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        resp.headers["Pragma"] = "no-cache"
        return resp

    await delete_stored_verify_invite(
        bot, invite_message_chat_id=invite_chat_id, invite_message_id=invite_message_id
    )
    await delete_stored_verify_join_message(
        bot, join_message_chat_id=join_chat_id, join_message_id=join_message_id
    )
    if post_verify_behavior == "welcome":
        await send_post_verify_welcome(
            bot,
            chat_id=welcome_chat_id,
            user_id=welcome_user_id,
            template=welcome_template,
        )
    ok = web.json_response({"ok": True, "correct": correct_n, "total": total_n})
    ok.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    ok.headers["Pragma"] = "no-cache"
    return ok
