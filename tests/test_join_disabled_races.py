"""Disabling verification must stop each pending stage of the join pipeline."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import RestrictChatMember
from sqlalchemy import select

from anticheatbot import telegram_retry
from anticheatbot.db.models import GroupSettings, UserChatState, VerifyToken
from anticheatbot.db.session import session_scope
from anticheatbot.handlers import verify_flow
from anticheatbot.services.groups import member_mute_pending
from anticheatbot.telegram_perms import muted_member_permissions

from tests.test_join_recovery_audit import CHAT, USER, _prepare


async def _close(bot):
    # Commit without needing the token/member lock held by the join pipeline.
    async with session_scope() as session:
        (await session.get(GroupSettings, CHAT)).verification_enabled = False
    await verify_flow.schedule_disabled_verifications(bot, CHAT)


async def _assert_state(*, finished, invitation=False):
    async with session_scope() as session:
        token = (await session.scalars(select(VerifyToken))).one()
        state = await session.get(UserChatState, (CHAT, USER))
        assert (token.used_at is not None) is finished
        assert state.verified is finished
        assert (token.invite_message_id == 77) is invitation
    assert not member_mute_pending(CHAT, USER)


def _assert_only_provisional_and_disabled_timers():
    delays = [call.kwargs["delay"] for call in verify_flow.schedule_verification_timeout.call_args_list]
    assert delays == [120 + verify_flow._VERIFY_PREPARATION_SECONDS, 0]


@pytest.mark.parametrize("stage", ["before_mute", "member_rpc", "mute_rpc", "retry_wait"])
def test_close_during_mute_pipeline_stops_invitation(monkeypatch, tmp_path, stage):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        restricted = False
        closed = False
        permissions = []
        try:
            async def close_once():
                nonlocal closed
                if not closed:
                    closed = True
                    await _close(bot)

            async def member(*args, **kwargs):
                if stage == "member_rpc":
                    await close_once()
                return SimpleNamespace(status="restricted" if restricted else "member", is_member=True)

            async def restrict(*args, **kwargs):
                nonlocal restricted
                permitted = kwargs["permissions"].can_send_messages
                permissions.append(permitted)
                if not permitted and stage == "retry_wait":
                    raise TelegramRetryAfter(
                        method=RestrictChatMember(chat_id=CHAT, user_id=USER, permissions=muted_member_permissions()),
                        message="flood", retry_after=1,
                    )
                restricted = not permitted
                if not permitted and stage == "mute_rpc":
                    await close_once()

            async def retry_wait(delay):
                assert delay >= 1
                await close_once()

            bot.get_chat_member.side_effect = member
            bot.restrict_chat_member.side_effect = restrict
            monkeypatch.setattr(telegram_retry, "asyncio", SimpleNamespace(sleep=retry_wait))
            if stage == "before_mute":
                original_mute = verify_flow._mute_pending_token

                async def close_before_mute(*args, **kwargs):
                    await close_once()
                    return await original_mute(*args, **kwargs)

                monkeypatch.setattr(verify_flow, "_mute_pending_token", close_before_mute)
            await verify_flow._start_verification_for_member(bot, chat_id=CHAT, user_id=USER, chat_title="Audit")
            invite.assert_not_awaited()
            assert permissions == {
                "before_mute": [], "member_rpc": [], "mute_rpc": [False, True], "retry_wait": [False],
            }[stage]
            assert not restricted
            await _assert_state(finished=True)
            _assert_only_provisional_and_disabled_timers()
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("restore_fails", [False, True])
def test_close_during_invitation_releases_and_deletes_without_resetting_timer(monkeypatch, tmp_path, restore_fails):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        restricted = False
        permissions = []
        try:
            async def member(*args, **kwargs):
                return SimpleNamespace(status="restricted" if restricted else "member", is_member=True)

            async def restrict(*args, **kwargs):
                nonlocal restricted
                permitted = kwargs["permissions"].can_send_messages
                permissions.append(permitted)
                if permitted and restore_fails:
                    raise TelegramNetworkError(
                        method=RestrictChatMember(chat_id=CHAT, user_id=USER, permissions=kwargs["permissions"]),
                        message="network unavailable",
                    )
                restricted = not permitted

            async def send_invite(*args, **kwargs):
                assert restricted
                await _close(bot)
                return SimpleNamespace(chat=SimpleNamespace(id=CHAT), message_id=77)

            bot.get_chat_member.side_effect = member
            bot.restrict_chat_member.side_effect = restrict
            invite.side_effect = send_invite
            await verify_flow._start_verification_for_member(bot, chat_id=CHAT, user_id=USER, chat_title="Audit")
            assert permissions == [False, True]
            invite.assert_awaited_once()
            bot.delete_message.assert_awaited_once_with(CHAT, 77)
            await _assert_state(finished=not restore_fails, invitation=True)
            _assert_only_provisional_and_disabled_timers()
            if restore_fails:
                # The retained token is accepted by the existing recovery worker.
                bot.restrict_chat_member.side_effect = None
                async with session_scope() as session:
                    token = (await session.scalars(select(VerifyToken))).one().token
                await verify_flow._expire_verification(bot, chat_id=CHAT, user_id=USER, token=token, kick=False)
                await _assert_state(finished=True, invitation=True)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_close_after_mute_with_network_failure_keeps_recoverable_token(monkeypatch, tmp_path):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        restricted = False
        permissions = []
        waits = []
        try:
            async def member(*args, **kwargs):
                return SimpleNamespace(status="restricted" if restricted else "member", is_member=True)

            async def restrict(*args, **kwargs):
                nonlocal restricted
                permitted = kwargs["permissions"].can_send_messages
                permissions.append(permitted)
                if permitted:
                    raise TelegramNetworkError(
                        method=RestrictChatMember(chat_id=CHAT, user_id=USER, permissions=kwargs["permissions"]),
                        message="network unavailable",
                    )
                restricted = True
                await _close(bot)

            async def wait(delay):
                waits.append(delay)

            bot.get_chat_member.side_effect = member
            bot.restrict_chat_member.side_effect = restrict
            monkeypatch.setattr(telegram_retry, "asyncio", SimpleNamespace(sleep=wait))
            await verify_flow._start_verification_for_member(bot, chat_id=CHAT, user_id=USER, chat_title="Audit")
            # Bounded retries only attempt restoration after close; never mute again.
            assert permissions == [False, True, True, True, True]
            assert waits == [1, 2]
            invite.assert_not_awaited()
            await _assert_state(finished=False)
            _assert_only_provisional_and_disabled_timers()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_close_during_failed_group_invite_does_not_send_dm_fallback(monkeypatch, tmp_path):
    async def run():
        engine, bot, invite = await _prepare(monkeypatch, tmp_path)
        try:
            async def group_invite(*args, **kwargs):
                await _close(bot)
                bot.get_chat_member.return_value = SimpleNamespace(status="restricted", is_member=True)
                return None

            invite.side_effect = group_invite
            await verify_flow._start_verification_for_member(bot, chat_id=CHAT, user_id=USER, chat_title="Audit")
            invite.assert_awaited_once()
            permissions = [call.kwargs["permissions"].can_send_messages for call in bot.restrict_chat_member.await_args_list]
            assert permissions == [False, True]
            await _assert_state(finished=True)
            _assert_only_provisional_and_disabled_timers()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_invitation_deadline_cannot_replace_queued_immediate_release(monkeypatch):
    async def run():
        expire = AsyncMock()
        monkeypatch.setattr(verify_flow, "_expire_verification", expire)
        monkeypatch.setattr(verify_flow, "_timeout_semaphore", asyncio.Semaphore(4))
        args = dict(chat_id=CHAT, user_id=USER, token="queued-disabled-release", kick=False)
        try:
            # An immediate worker cannot enter until the join has finished sending
            # its invitation and committing. A subsequent normal deadline must lose.
            async with verify_flow._member_lock(CHAT, USER):
                verify_flow.schedule_verification_timeout(None, delay=0, **args)
                await asyncio.sleep(0)
                immediate = verify_flow._verification_tasks_by_token[args["token"]]
                assert immediate not in verify_flow._active_verification_tasks
                verify_flow.schedule_verification_timeout(None, delay=120, **args)
                assert verify_flow._verification_tasks_by_token[args["token"]] is immediate
            await asyncio.wait_for(immediate, timeout=1)
            expire.assert_awaited_once_with(None, **args)
            assert immediate not in verify_flow._immediate_verification_tasks
        finally:
            await verify_flow.stop_verification_timeouts()
    asyncio.run(run())
