from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from aiohttp import web
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from anticheatbot.config import Settings
from anticheatbot.db.models import GroupSettings, QuizQuestion, UserChatState, VerifyQuizSession, VerifyToken
from anticheatbot.db.session import session_scope
from anticheatbot.init_data import InitDataInvalidError, parse_language_code, parse_user_id, validate_init_data
from anticheatbot.locale_data import normalize_locale
from anticheatbot.services.audit import log_event
from anticheatbot.services.groups import get_or_create_group_settings
from anticheatbot.services.quiz import QuizError, draw_quiz_for_token, grade_answers
from anticheatbot.telegram_perms import normal_member_permissions
from anticheatbot.translation_service import translate_or_same
from anticheatbot.turnstile import verify_turnstile
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


def _target_locale(request: web.Request, init_fields: dict[str, Any]) -> str:
    q = request.rel_url.query.get("lang")
    if q:
        return normalize_locale(q)
    return normalize_locale(parse_language_code(init_fields))


async def verify_session(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    token_str = request.rel_url.query.get("t")
    if not token_str:
        raise web.HTTPBadRequest(text="missing t")

    async with session_scope() as session:
        vt = await session.get(VerifyToken, token_str)
        if vt is None or vt.user_id != user_id:
            raise web.HTTPForbidden(text="bad token")
        if vt.used_at is not None:
            raise web.HTTPForbidden(text="token used")
        if vt.expires_at < datetime.now(tz=UTC):
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
            "quiz_pass_score_threshold": gs.quiz_pass_score_threshold,
        }

        if gs.verify_mode == "quiz":
            existing = await session.get(VerifyQuizSession, token_str)
            if existing is None:
                try:
                    drawn = await draw_quiz_for_token(session, chat_id=vt.chat_id, token=token_str, gs=gs)
                except QuizError as e:
                    raise web.HTTPBadRequest(text=str(e)) from e
                questions = drawn.questions
            else:
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
                choices_out: list[str] = []
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
                    choices_out.append(tr_ch)
                    ch_hit_row.append(hit_c)
                hits.append(hit_p and all(ch_hit_row))
                out_quiz.append({"id": q.id, "prompt": tr_prompt, "choices": choices_out})
            payload["quiz"] = out_quiz
            payload["translation_cache_hit_quiz"] = all(hits) if hits else True

        return web.json_response(payload)


async def _turnstile_or_403(settings: Settings, gs: GroupSettings, token: str | None, remote_ip: str | None) -> None:
    if not gs.turnstile_enabled:
        return
    secret = settings.turnstile_secret_key
    if not secret:
        raise web.HTTPInternalServerError(text="turnstile enabled but TURNSTILE_SECRET_KEY missing")
    if not token or not await verify_turnstile(secret=secret, response_token=token, remote_ip=remote_ip):
        raise web.HTTPForbidden(text="turnstile failed")


async def verify_rules_complete(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    bot: Bot = request.app["bot"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    body = await json_object(request)
    token_str = body.get("token")
    if not token_str:
        raise web.HTTPBadRequest(text="missing token")
    turnstile_tok = body.get("turnstile_token")
    remote_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote

    async with session_scope() as session:
        vt = await session.get(VerifyToken, token_str)
        if vt is None or vt.user_id != user_id:
            raise web.HTTPForbidden(text="bad token")
        if vt.used_at is not None:
            raise web.HTTPForbidden(text="token used")
        if vt.expires_at < datetime.now(tz=UTC):
            raise web.HTTPForbidden(text="token expired")

        gs = await get_or_create_group_settings(session, vt.chat_id)
        if gs.verify_mode != "rules_ack":
            raise web.HTTPBadRequest(text="wrong verify_mode")
        await _turnstile_or_403(settings, gs, turnstile_tok, remote_ip)

        vt.used_at = datetime.now(tz=UTC)
        ucs = await session.get(UserChatState, (vt.chat_id, vt.user_id))
        if ucs is None:
            ucs = UserChatState(chat_id=vt.chat_id, user_id=vt.user_id, verified=True, verified_at=datetime.now(tz=UTC))
            session.add(ucs)
        else:
            ucs.verified = True
            ucs.verified_at = datetime.now(tz=UTC)
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

    return web.json_response({"ok": True})


async def verify_quiz_submit(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    bot: Bot = request.app["bot"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    body = await json_object(request)
    token_str = body.get("token")
    answers_raw = body.get("answers")
    if not token_str or not isinstance(answers_raw, dict):
        raise web.HTTPBadRequest(text="missing token or answers")
    answers: dict[int, int] = {}
    try:
        for k, v in answers_raw.items():
            answers[int(k)] = int(v)
    except (TypeError, ValueError) as e:
        raise web.HTTPBadRequest(text="invalid answers") from e
    turnstile_tok = body.get("turnstile_token")
    remote_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote

    async with session_scope() as session:
        vt = await session.get(VerifyToken, token_str)
        if vt is None or vt.user_id != user_id:
            raise web.HTTPForbidden(text="bad token")
        if vt.used_at is not None:
            raise web.HTTPForbidden(text="token used")
        if vt.expires_at < datetime.now(tz=UTC):
            raise web.HTTPForbidden(text="token expired")

        gs = await get_or_create_group_settings(session, vt.chat_id)
        if gs.verify_mode != "quiz":
            raise web.HTTPBadRequest(text="wrong verify_mode")
        await _turnstile_or_403(settings, gs, turnstile_tok, remote_ip)

        try:
            score, max_score = await grade_answers(session, token=token_str, answers=answers)
        except QuizError as e:
            raise web.HTTPBadRequest(text=str(e)) from e

        if score < gs.quiz_pass_score_threshold:
            await log_event(
                session,
                event_type="verify_quiz_fail",
                chat_id=vt.chat_id,
                user_id=vt.user_id,
                payload={"score": score, "max": max_score},
            )
            return web.json_response(
                {
                    "ok": False,
                    "score": score,
                    "max_score": max_score,
                    "need": gs.quiz_pass_score_threshold,
                },
                status=403,
            )

        vt.used_at = datetime.now(tz=UTC)
        ucs = await session.get(UserChatState, (vt.chat_id, vt.user_id))
        if ucs is None:
            ucs = UserChatState(chat_id=vt.chat_id, user_id=vt.user_id, verified=True, verified_at=datetime.now(tz=UTC))
            session.add(ucs)
        else:
            ucs.verified = True
            ucs.verified_at = datetime.now(tz=UTC)
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
            payload={"score": score},
        )

    return web.json_response({"ok": True, "score": score})
