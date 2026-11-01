"""A slow disabled-verification release must not act on a newer membership."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from anticheatbot.db.models import GroupSettings, UserChatState, VerifyToken
from anticheatbot.db.session import session_scope
from anticheatbot.handlers import verify_flow
from anticheatbot.services.groups import set_member_verification_exempt
from anticheatbot.time_utils import utc_now
from tests.test_join_event_idempotency import _events, _join, _live_token
from tests.test_join_recovery_audit import CHAT, USER, _prepare


@pytest.mark.parametrize("change", ["new_join", "promoted", "departed"])
def test_disabled_release_rechecks_events_started_during_member_rpc(monkeypatch, tmp_path, change):
    async def run():
        engine, bot, _ = await _prepare(monkeypatch, tmp_path)
        changed_task = None
        try:
            date = utc_now().replace(microsecond=0)
            event, notice = _events(date)
            await _join("member", event, notice, bot, 100)
            token = await _live_token()
            bot.restrict_chat_member.reset_mock()
            async with session_scope() as session:
                (await session.get(GroupSettings, CHAT)).verification_enabled = False

            async def slow_membership_read(*args):
                nonlocal changed_task
                newer, _ = _events(date + timedelta(seconds=1))
                if change == "new_join":
                    callback = verify_flow.on_user_joined
                else:
                    newer.new_chat_member.status = "administrator" if change == "promoted" else "left"
                    callback = verify_flow.on_member_promoted if change == "promoted" else verify_flow.on_member_departed
                changed_task = asyncio.create_task(callback(
                    newer, bot, event_update=SimpleNamespace(update_id=101),
                ))
                await asyncio.sleep(0)
                # Timeout recovery owns the member lock, so only the early event context
                # is available until it returns. The old RPC result is intentionally stale.
                assert not changed_task.done()
                return SimpleNamespace(status="restricted", is_member=True)

            bot.get_chat_member.side_effect = slow_membership_read
            await asyncio.wait_for(verify_flow._verify_timeout_task(
                bot, chat_id=CHAT, user_id=USER, token=token, delay=0, kick=False,
            ), timeout=2)
            bot.restrict_chat_member.assert_not_awaited()
            await asyncio.wait_for(changed_task, timeout=2)
            bot.restrict_chat_member.assert_not_awaited()
            async with session_scope() as session:
                assert (await session.get(VerifyToken, token)).used_at is not None
                state = await session.get(UserChatState, (CHAT, USER))
                assert state.verified is (change != "departed")
        finally:
            if changed_task is not None and not changed_task.done():
                changed_task.cancel()
                await asyncio.gather(changed_task, return_exceptions=True)
            set_member_verification_exempt(CHAT, USER, False)
            await engine.dispose()
    asyncio.run(run())
