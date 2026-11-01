"""Independent regressions for early interception and migrated membership state."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import BanChatMember
from sqlalchemy import select

from anticheatbot.db.models import GroupSettings, UserChatState, VerifyQuizSession, VerifyToken
from anticheatbot.db.session import session_scope
from anticheatbot.handlers import guest_bots, verify_flow
from anticheatbot.services.groups import set_member_verification_exempt
from anticheatbot.time_utils import utc_now
from anticheatbot.web import verify_api
from tests.test_join_event_idempotency import _events, _join, _live_token, _request
from tests.test_join_recovery_audit import CHAT, USER, _prepare


async def _intercept_message(bot, *, expected):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=CHAT, type="supergroup"),
        from_user=SimpleNamespace(id=USER, is_bot=False), message_id=998,
    )
    intercepted = await guest_bots._PendingVerificationMessageFilter()(message)
    if intercepted:
        await guest_bots.on_pending_verification_message(message, bot)
    assert intercepted is expected
    if expected:
        bot.delete_message.assert_awaited_once_with(CHAT, 998)
    else:
        bot.delete_message.assert_not_awaited()


@pytest.mark.parametrize("source", ["member", "notice"])
def test_delayed_notice_never_intercepts_approved_member_during_inviter_lookup(monkeypatch, tmp_path, source):
    async def run():
        engine, bot, _ = await _prepare(monkeypatch, tmp_path)
        join_task = waiter = None
        release = asyncio.Event()
        try:
            event, notice = _events(utc_now().replace(microsecond=0))
            inviter = SimpleNamespace(id=USER + 10, is_bot=False)
            event.from_user = notice.from_user = inviter
            await _join("notice" if source == "member" else "member", event, notice, bot, 100)
            token = await _live_token()
            bot.get_chat_member.return_value = SimpleNamespace(status="restricted", is_member=True)
            assert (await verify_api.verify_rules_complete(_request(bot, token))).status == 200
            bot.delete_message.reset_mock()
            entered = asyncio.Event()

            async def slow_inviter(*args):
                entered.set()
                await release.wait()
                return False

            monkeypatch.setattr(verify_flow, "_inviter_is_admin", slow_inviter)
            join_task = asyncio.create_task(_join(source, event, notice, bot, 101))
            waiter = asyncio.create_task(entered.wait())
            done, _ = await asyncio.wait({join_task, waiter}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
            assert done
            if join_task in done:
                await join_task  # Skipping the unnecessary inviter lookup is valid too.
            await _intercept_message(bot, expected=False)
            release.set()
            await asyncio.wait_for(join_task, timeout=2)
            async with session_scope() as session:
                assert (await session.get(UserChatState, (CHAT, USER))).verified
                assert len((await session.scalars(select(VerifyToken))).all()) == 1
        finally:
            release.set()
            for task in (join_task, waiter):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("source", ["member", "notice"])
@pytest.mark.parametrize("previous_state", ["new", "approved", "former_admin"])
def test_real_join_still_intercepts_before_inviter_lookup_completes(monkeypatch, tmp_path, source, previous_state):
    async def run():
        engine, bot, _ = await _prepare(monkeypatch, tmp_path)
        join_task = None
        release = asyncio.Event()
        try:
            if previous_state != "new":
                async with session_scope() as session:
                    session.add(UserChatState(
                        chat_id=CHAT, user_id=USER, verified=True,
                        verified_at=utc_now() - timedelta(days=1),
                        membership_status="admin" if previous_state == "former_admin" else None,
                        membership_event_at=utc_now() - timedelta(days=1) if previous_state == "former_admin" else None,
                        membership_update_id=50 if previous_state == "former_admin" else None,
                    ))
                if previous_state == "former_admin":
                    set_member_verification_exempt(CHAT, USER, True)
            event, notice = _events(utc_now().replace(microsecond=0))
            event.from_user = notice.from_user = SimpleNamespace(id=USER + 10, is_bot=False)
            entered = asyncio.Event()

            async def slow_inviter(*args):
                entered.set()
                await release.wait()
                return False

            monkeypatch.setattr(verify_flow, "_inviter_is_admin", slow_inviter)
            join_task = asyncio.create_task(_join(source, event, notice, bot, 100))
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert not join_task.done()
            await _intercept_message(bot, expected=True)
            release.set()
            await asyncio.wait_for(join_task, timeout=2)
            assert await _live_token()
            async with session_scope() as session:
                assert (await session.get(UserChatState, (CHAT, USER))).verified is False
        finally:
            release.set()
            if join_task is not None and not join_task.done():
                join_task.cancel()
                await asyncio.gather(join_task, return_exceptions=True)
            set_member_verification_exempt(CHAT, USER, False)
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["quiz_failed", "timed_out"])
@pytest.mark.parametrize("source", ["member", "notice"])
def test_legacy_consumed_attempt_is_not_reopened_by_its_late_join_notice(monkeypatch, tmp_path, outcome, source):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        try:
            joined = utc_now().replace(microsecond=0) - timedelta(seconds=10)
            token = "legacy_verification_token_12345678"
            async with session_scope() as session:
                gs = await session.get(GroupSettings, CHAT)
                gs.verify_mode = "quiz"
                gs.kick_on_verify_timeout = False
                session.add(UserChatState(chat_id=CHAT, user_id=USER, verified=False))
                session.add(VerifyToken(
                    token=token, chat_id=CHAT, user_id=USER, created_at=joined + timedelta(milliseconds=500),
                    expires_at=utc_now() + timedelta(minutes=5),
                ))
                session.add(VerifyQuizSession(token=token, question_ids_json=[1], shuffle_maps_json={
                    "1": [0, 1], "__snapshot_v1": {
                        "version": "legacy-version", "required_correct": 1,
                        "questions": [{"id": 1, "prompt": "Choose A", "choices": ["A", "B"], "correct_index": 0}],
                    },
                }))
            bot.get_chat_member.return_value = SimpleNamespace(status="restricted", is_member=True)
            bot.ban_chat_member = AsyncMock(side_effect=TelegramForbiddenError(
                method=BanChatMember(chat_id=CHAT, user_id=USER), message="permission removed",
            ))
            bot.unban_chat_member = AsyncMock()
            if outcome == "quiz_failed":
                request = _request(bot, token)
                request.json.return_value.update(answers={"1": 1}, quiz_version="legacy-version")
                assert (await verify_api.verify_quiz_submit(request)).status == 403
                bot.ban_chat_member.assert_awaited_once()
            else:
                async with session_scope() as session:
                    (await session.get(VerifyToken, token)).expires_at = utc_now() - timedelta(seconds=1)
                await verify_flow._expire_verification(bot, chat_id=CHAT, user_id=USER, token=token, kick=False)
            async with session_scope() as session:
                assert (await session.get(VerifyToken, token)).used_at is not None
            event, notice = _events(joined)
            await _join(source, event, notice, bot, 100)
            invite.assert_not_awaited()
            bot.restrict_chat_member.assert_not_awaited()
            async with session_scope() as session:
                tokens = (await session.scalars(select(VerifyToken))).all()
                assert len(tokens) == 1 and tokens[0].used_at is not None
                assert (await session.get(UserChatState, (CHAT, USER))).verified is False
            departed = SimpleNamespace(
                chat=event.chat, date=joined + timedelta(seconds=1), old_chat_member=event.new_chat_member,
                new_chat_member=SimpleNamespace(status="left", user=event.new_chat_member.user),
            )
            await verify_flow.on_member_departed(departed, bot, event_update=SimpleNamespace(update_id=101))
            new_event, new_notice = _events(joined + timedelta(seconds=2))
            await _join(source, new_event, new_notice, bot, 102)
            assert await _live_token() != token
            invite.assert_awaited_once()
            bot.restrict_chat_member.assert_awaited_once()
        finally:
            await engine.dispose()
    asyncio.run(run())
