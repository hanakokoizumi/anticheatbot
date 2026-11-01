from __future__ import annotations

import ipaddress
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
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
    release_disabled_verification,
    send_post_verify_welcome,
    verification_is_current,
    verification_state_is_current,
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
from anticheatbot.services.quiz import (
    QuizChangedError, QuizError, draw_quiz_for_token, grade_answers, normalize_shuffle_order, quiz_from_snapshot,
)
from anticheatbot.telegram_perms import normal_member_permissions
from anticheatbot.time_utils import coerce_utc, utc_now
from anticheatbot.translation_service import translate_or_same
from anticheatbot.turnstile import TurnstileUnavailableError, verify_turnstile
from anticheatbot.web.json_body import json_object

log = logging.getLogger(__name__)

# DB column verify_tokens.token is VARCHAR(64); minted with secrets.token_urlsafe(24) (~32 chars).
_VERIFY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_MAX_QUIZ_ANSWER_KEYS = 128
_MAX_LANG_QUERY_LEN = 32  # LLMTranslationCache target_locale VARCHAR width.
_TRANSLATION_BUDGET_SECONDS = 8.0


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
    try:
        addr = ipaddress.ip_address(r)
    except ValueError:
        return None
    # A reverse proxy/container address is not the visitor's IP. Siteverify's remoteip is optional.
    return r if addr.is_global else None


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
        fields = validate_init_data(
            raw,
            bot_token=settings.bot_token,
            max_age_seconds=settings.init_data_max_age_seconds,
        )
        parse_user_id(fields)
        return fields
    except InitDataInvalidError as e:
        raise web.HTTPForbidden(text=str(e)) from e


def _target_locale(request: web.Request, init_fields: dict[str, Any]) -> str:
    q = request.rel_url.query.get("lang")
    if q:
        q = str(q).strip()
        if len(q) > _MAX_LANG_QUERY_LEN:
            q = q[:_MAX_LANG_QUERY_LEN]
        return normalize_locale(q)
    return normalize_locale((parse_language_code(init_fields) or "")[:_MAX_LANG_QUERY_LEN])


def _welcome_template_for_user(gs: GroupSettings, init_fields: dict[str, Any]) -> str:
    """Custom welcome body, or localized default from Telegram user.language_code in initData."""
    custom = (gs.welcome_message_markdown or "").strip()
    if custom:
        return custom
    loc = resolve_bot_locale(parse_language_code(init_fields))
    return bot_t(loc, "default_welcome")


async def _disabled_verification_response(
    session: AsyncSession, bot: Bot, vt: VerifyToken, *, consumed_here: bool = False,
) -> web.Response | None:
    try:
        completed = await release_disabled_verification(session, bot, vt, consumed_here=consumed_here)
    except TelegramAPIError as exc:
        raise web.HTTPServiceUnavailable(text="unable to restore chat permissions; please retry") from exc
    if not completed:
        return None
    await session.commit()
    await delete_stored_verify_invite(bot, invite_message_chat_id=vt.invite_message_chat_id, invite_message_id=vt.invite_message_id)
    await delete_stored_verify_join_message(bot, join_message_chat_id=vt.join_message_chat_id, join_message_id=vt.join_message_id)
    return web.json_response({"ok": True, "verification_disabled": True})


async def _challenge_or_disabled(
    session: AsyncSession, bot: Bot, vt: VerifyToken, settings: Settings, gs: GroupSettings,
    turnstile_token: object, remote_ip: str | None,
) -> web.Response | None:
    try:
        await _turnstile_or_403(settings, gs, turnstile_token, remote_ip)
    except web.HTTPException:
        disabled = await _disabled_verification_response(session, bot, vt)
        if disabled is not None:
            return disabled
        raise
    return await _disabled_verification_response(session, bot, vt)


async def verify_session(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    init_fields = _parse_init(request, settings)
    user_id = parse_user_id(init_fields)
    token_str = _parse_verify_token_query(request.rel_url.query.get("t"))

    async with session_scope() as session:
        vt = await session.get(VerifyToken, token_str, with_for_update=True)
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
        disabled = await _disabled_verification_response(session, request.app["bot"], vt)
        if disabled is not None:
            return disabled
        if vt.used_at is not None:
            raise web.HTTPForbidden(text="token used")
        if coerce_utc(vt.expires_at) < utc_now():
            raise web.HTTPForbidden(text="token expired")

        gs = await get_or_create_group_settings(session, vt.chat_id)
        _check_turnstile_configuration(settings, gs)
        target = _target_locale(request, init_fields)
        questions: list[QuizQuestion] = []
        shuffle_maps: dict[str, Any] = {}
        quiz_version: str | None = None
        quiz_required_correct = gs.quiz_pass_min_correct
        if gs.verify_mode == "quiz":
            existing = await session.get(VerifyQuizSession, token_str)
            drawn = quiz_from_snapshot(existing) if existing is not None else None
            if drawn is None:
                if existing is not None:
                    await session.delete(existing)
                    await session.flush()
                try:
                    drawn = await draw_quiz_for_token(session, chat_id=vt.chat_id, token=token_str, gs=gs)
                except QuizError as e:
                    raise web.HTTPBadRequest(text=str(e)) from e
            questions = drawn.questions
            shuffle_maps = drawn.shuffle_maps
            quiz_version = drawn.version
            quiz_required_correct = drawn.required_correct
        # Persist the fixed draw and release the token lock before potentially slow translations.
        # Submit and timeout handlers use the same lock to decide which outcome wins.
        await session.commit()
        chat_id = vt.chat_id
        expires_at = coerce_utc(vt.expires_at)
        payload: dict[str, Any] = {
            "verify_mode": gs.verify_mode,
            "rules_markdown": gs.rules_markdown,
            "turnstile_site_key": settings.turnstile_site_key if gs.turnstile_enabled else None,
            "turnstile_enabled": gs.turnstile_enabled,
            "resolved_locale": target,
            "translation_cache_hit_rules": True,
            "quiz_pass_min_correct": quiz_required_correct,
            "quiz_draw_count": gs.quiz_draw_count,
        }

        if gs.verify_mode == "quiz":
            payload["quiz"] = [
                {
                    "id": q.id, "prompt": q.prompt,
                    "choices": [q.choices_json[i] for i in normalize_shuffle_order(shuffle_maps, int(q.id), len(q.choices_json))],
                }
                for q in questions
            ]
            payload["quiz_version"] = quiz_version
            payload["translation_cache_hit_quiz"] = True

        async def translate_one(*, namespace: str, entity_id: str, text: str) -> tuple[str, bool]:
            result = await translate_or_same(
                session, settings, gs, namespace=namespace, entity_id=entity_id,
                chat_id=chat_id, text=text, target_locale=target,
            )
            # Never hold a translation cache write lock across the next model request.
            await session.commit()
            return result

        async def translate_payload() -> None:
            payload["rules_markdown"], payload["translation_cache_hit_rules"] = await translate_one(
                namespace="rules", entity_id="rules", text=gs.rules_markdown,
            )
            hits: list[bool] = []
            for q in questions:
                tr_prompt, hit_p = await translate_one(
                    namespace="quiz_prompt", entity_id=str(q.id), text=q.prompt,
                )
                choices_orig: list[str] = []
                ch_hit_row: list[bool] = []
                for i, ch in enumerate(q.choices_json):
                    tr_ch, hit_c = await translate_one(
                        namespace="quiz_choice", entity_id=f"{q.id}:{i}", text=ch,
                    )
                    choices_orig.append(tr_ch)
                    ch_hit_row.append(hit_c)
                order = normalize_shuffle_order(shuffle_maps, int(q.id), len(choices_orig))
                choices_out = [choices_orig[order[i]] for i in range(len(order))]
                hits.append(hit_p and all(ch_hit_row))
                payload["quiz"][len(hits)-1] = {"id": q.id, "prompt": tr_prompt, "choices": choices_out}
            if "quiz" in payload:
                payload["translation_cache_hit_quiz"] = all(hits) if hits else True

        # Reserve most of the remaining deadline for reading and answering the quiz.
        # Slow or large translations fall back to the already prepared source content.
        remaining = max(0, (expires_at - utc_now()).total_seconds())
        try:
            async with asyncio.timeout(min(_TRANSLATION_BUDGET_SECONDS, remaining / 4)):
                await translate_payload()
        except TimeoutError:
            await session.rollback()
            payload["translation_fallback"] = "timeout"
            payload["translation_cache_hit_rules"] = False
            if "quiz" in payload:
                payload["translation_cache_hit_quiz"] = False
            log.warning("verification translation budget exceeded chat=%s", chat_id)

        # The token may have been completed or expired while model calls were running.
        current = await session.get(VerifyToken, token_str, populate_existing=True, with_for_update=True)
        if current is not None:
            disabled = await _disabled_verification_response(session, request.app["bot"], current)
            if disabled is not None:
                return disabled
        if current is None or current.used_at is not None:
            raise web.HTTPForbidden(text="token used")
        if coerce_utc(current.expires_at) <= utc_now():
            raise web.HTTPForbidden(text="token expired")

        resp = web.json_response(payload)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        resp.headers["Pragma"] = "no-cache"
        return resp


def _check_turnstile_configuration(settings: Settings, gs: GroupSettings) -> None:
    if gs.turnstile_enabled and not (
        (settings.turnstile_site_key or "").strip() and (settings.turnstile_secret_key or "").strip()
    ):
        raise web.HTTPServiceUnavailable(
            text=json.dumps({"error": "turnstile_not_configured"}), content_type="application/json"
        )


async def _turnstile_or_403(settings: Settings, gs: GroupSettings, token: object, remote_ip: str | None) -> None:
    if not gs.turnstile_enabled:
        return
    _check_turnstile_configuration(settings, gs)
    secret = (settings.turnstile_secret_key or "").strip()
    tok: str | None
    if token is None:
        tok = None
    elif isinstance(token, str):
        tok = token.strip() or None
        if tok and len(tok) > 2048:
            raise web.HTTPForbidden(text="turnstile failed")
    else:
        tok = None
    if not tok:
        raise web.HTTPForbidden(text="turnstile failed")
    try:
        valid = await verify_turnstile(
            secret=secret,
            response_token=tok,
            remote_ip=remote_ip,
            expected_hostname=urlsplit(settings.webapp_public_url).hostname,
        )
    except TurnstileUnavailableError as e:
        raise web.HTTPServiceUnavailable(
            text=json.dumps({"error": "turnstile_unavailable"}), content_type="application/json"
        ) from e
    if not valid:
        raise web.HTTPForbidden(text="turnstile failed")


async def _consume_verify_token_once(session: AsyncSession, *, token_str: str) -> datetime:
    """Single-use token: atomically set ``used_at`` so double-submit / parallel requests only succeed once."""
    now = utc_now()
    res = await session.execute(
        update(VerifyToken)
        .where(
            VerifyToken.token == token_str,
            VerifyToken.used_at.is_(None),
            VerifyToken.expires_at > now,
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        raise web.HTTPForbidden(text="token expired or used")
    return now


def _membership_changed() -> None:
    raise web.HTTPForbidden(text=json.dumps({"error": "membership_changed"}), content_type="application/json")


async def _require_current_membership(
    session: AsyncSession, vt: VerifyToken, *, allow_verified: bool = False,
) -> UserChatState | None:
    current, state = await verification_is_current(session, vt, allow_verified=allow_verified)
    if not current:
        _membership_changed()
    return state


async def _restore_member_permissions(bot: Bot, *, session: AsyncSession, vt: VerifyToken) -> None:
    try:
        await _require_current_membership(session, vt, allow_verified=True)
        member = await bot.get_chat_member(vt.chat_id, vt.user_id)
        state = await _require_current_membership(session, vt, allow_verified=True)
        if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) or (
            member.status == ChatMemberStatus.RESTRICTED and not member.is_member
        ):
            _membership_changed()
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR, ChatMemberStatus.MEMBER):
            return
        if not verification_state_is_current(vt, state, allow_verified=True):
            _membership_changed()
        await bot.restrict_chat_member(
            vt.chat_id, vt.user_id, permissions=normal_member_permissions(), use_independent_chat_permissions=True,
        )
    except TelegramAPIError as e:
        log.warning("unrestrict failed: %s", e)
        raise web.HTTPServiceUnavailable(text="unable to restore chat permissions; please retry") from e


async def _kick_failed_quiz_member(bot: Bot, *, vt: VerifyToken, session: AsyncSession, gs: GroupSettings) -> bool:
    try:
        await _require_current_membership(session, vt)
        member = await bot.get_chat_member(vt.chat_id, vt.user_id)
        state = await _require_current_membership(session, vt)
        # The administrator may have disabled verification while Telegram was slow.
        await session.refresh(gs)
        if not verification_state_is_current(vt, state):
            _membership_changed()
        if not gs.verification_enabled:
            return False
        if member.status in (
            ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR,
        ) or (member.status == ChatMemberStatus.RESTRICTED and not member.is_member):
            return True
        await register_recent_bot_kick(chat_id=vt.chat_id, user_id=vt.user_id)
        if not verification_state_is_current(vt, state):
            _membership_changed()
        # If the immediate unban fails, expire this temporary ban instead of leaving it permanent.
        await bot.ban_chat_member(vt.chat_id, vt.user_id, until_date=utc_now() + timedelta(seconds=60))
        await bot.unban_chat_member(vt.chat_id, vt.user_id, only_if_banned=True)
    except TelegramAPIError as e:
        log.warning("kick on verify quiz fail failed: %s", e)
        state = await _require_current_membership(session, vt)
        await session.refresh(gs)
        if not verification_state_is_current(vt, state):
            _membership_changed()
        if not gs.verification_enabled:
            return False
    return True


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
        vt = await session.get(VerifyToken, token_str, with_for_update=True)
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
        disabled = await _disabled_verification_response(session, bot, vt)
        if disabled is not None:
            return disabled
        if vt.used_at is not None:
            raise web.HTTPForbidden(text="token used")
        if coerce_utc(vt.expires_at) < utc_now():
            raise web.HTTPForbidden(text="token expired")

        gs = await get_or_create_group_settings(session, vt.chat_id)
        if gs.verify_mode != "rules_ack":
            raise web.HTTPBadRequest(text="wrong verify_mode")
        disabled = await _challenge_or_disabled(session, bot, vt, settings, gs, turnstile_tok, remote_ip)
        if disabled is not None:
            return disabled

        await _require_current_membership(session, vt)
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

        await _restore_member_permissions(bot, session=session, vt=vt)
        disabled = await _disabled_verification_response(session, bot, vt, consumed_here=True)
        if disabled is not None:
            return disabled

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
    async with session_scope() as session:
        vt = await session.get(VerifyToken, token_str, with_for_update=True)
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
        disabled = await _disabled_verification_response(session, bot, vt)
        if disabled is not None:
            return disabled
        if vt.used_at is not None:
            raise web.HTTPForbidden(text="token used")
        if coerce_utc(vt.expires_at) < utc_now():
            raise web.HTTPForbidden(text="token expired")

        gs = await get_or_create_group_settings(session, vt.chat_id)
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
        if gs.verify_mode != "quiz":
            raise web.HTTPBadRequest(text="wrong verify_mode")
        disabled = await _challenge_or_disabled(session, bot, vt, settings, gs, turnstile_tok, remote_ip)
        if disabled is not None:
            return disabled

        await _require_current_membership(session, vt)
        try:
            correct_n, total_n, need = await grade_answers(
                session, token=token_str, answers=answers, quiz_version=body.get("quiz_version")
            )
        except QuizChangedError as e:
            raise web.HTTPConflict(
                text=json.dumps({
                    "error": "quiz_changed",
                    "message": "Quiz updated. Please close this page and reopen the verification link to answer again.",
                }),
                content_type="application/json",
            ) from e
        except QuizError as e:
            raise web.HTTPBadRequest(text=str(e)) from e

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
            vt.used_at = await _consume_verify_token_once(session, token_str=token_str)
            qs_fail = await session.get(VerifyQuizSession, token_str)
            if qs_fail is not None:
                await session.delete(qs_fail)
            await session.flush()
            kicked = await _kick_failed_quiz_member(bot, vt=vt, session=session, gs=gs)
            if not kicked:
                disabled = await _disabled_verification_response(session, bot, vt, consumed_here=True)
                if disabled is not None:
                    return disabled
                raise web.HTTPServiceUnavailable(text="verification settings changed; please retry")
            await log_event(
                session,
                event_type="verify_quiz_fail",
                chat_id=vt.chat_id,
                user_id=vt.user_id,
                payload={"correct": correct_n, "total": total_n, "need": need},
            )
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

            await _restore_member_permissions(bot, session=session, vt=vt)
            disabled = await _disabled_verification_response(session, bot, vt, consumed_here=True)
            if disabled is not None:
                return disabled

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
