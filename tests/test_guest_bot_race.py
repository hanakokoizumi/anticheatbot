from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from anticheatbot.db.models import AuditEvent, GroupSettings, KnownChat, UserChatState, VerifyToken
from anticheatbot.db.session import configure_engine, create_all, session_scope
from anticheatbot.handlers import guest_bots, verify_flow
from anticheatbot.services.groups import set_guest_bot_block_enabled
from anticheatbot.time_utils import utc_now


def _message(*, user_id: int, is_bot: bool = False, caller_id: int | None = None):
    caller = SimpleNamespace(id=caller_id) if caller_id is not None else None
    return SimpleNamespace(
        chat=SimpleNamespace(id=-100123, type="supergroup"),
        from_user=SimpleNamespace(id=user_id, is_bot=is_bot),
        message_id=42,
        guest_query_id=None,
        guest_bot_caller_user=caller,
        guest_bot_caller_chat=None,
    )


def test_pending_member_message_and_guest_reply_are_deleted(monkeypatch) -> None:
    async def run() -> None:
        set_guest_bot_block_enabled(-100123, False)
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await create_all()
        monkeypatch.setattr(
            guest_bots,
            "get_settings",
            lambda: SimpleNamespace(global_admin_id_set=set()),
        )
        async with session_scope() as session:
            session.add(KnownChat(chat_id=-100123, title="Test"))
            session.add(GroupSettings(chat_id=-100123, block_guest_bots=False))
            session.add(UserChatState(chat_id=-100123, user_id=55, verified=True))
            session.add(
                VerifyToken(
                    token="live-token",
                    chat_id=-100123,
                    user_id=55,
                    expires_at=utc_now() + timedelta(minutes=3),
                )
            )

        summon = _message(user_id=55)
        assert await guest_bots._PendingVerificationMessageFilter()(summon)
        bot = SimpleNamespace(delete_message=AsyncMock())
        await guest_bots.on_pending_verification_message(summon, bot)
        bot.delete_message.assert_awaited_once_with(-100123, 42)

        reply = _message(user_id=999, is_bot=True, caller_id=55)
        assert await guest_bots._BlockGuestBotsInGroupFilter()(reply)

        async with session_scope() as session:
            events = (await session.execute(select(AuditEvent))).scalars().all()
            assert [(event.event_type, event.user_id) for event in events] == [
                ("unverified_message_deleted", 55)
            ]
            token = await session.get(VerifyToken, "live-token")
            token.used_at = utc_now()

        assert not await guest_bots._PendingVerificationMessageFilter()(summon)
        assert not await guest_bots._BlockGuestBotsInGroupFilter()(reply)
        await engine.dispose()

    asyncio.run(run())


def test_enabled_guest_block_uses_immediate_path(monkeypatch) -> None:
    reply = _message(user_id=999, is_bot=True)
    reply.guest_bot_caller_chat = SimpleNamespace(id=-100123)
    set_guest_bot_block_enabled(-100123, True)
    db = AsyncMock(side_effect=AssertionError("enabled path must not query the database"))
    monkeypatch.setattr(guest_bots, "session_scope", db)
    try:
        assert asyncio.run(guest_bots._BlockGuestBotsInGroupFilter()(reply))
        db.assert_not_called()
        assert not asyncio.run(guest_bots._BlockGuestBotsInGroupFilter()(_message(user_id=55)))
    finally:
        set_guest_bot_block_enabled(-100123, False)


def test_join_persists_recovery_before_requesting_mute(monkeypatch) -> None:
    async def run() -> None:
        engine = configure_engine("sqlite+aiosqlite:///:memory:")
        await create_all()
        async with session_scope() as session:
            session.add(KnownChat(chat_id=-100123, title="Test"))
            session.add(GroupSettings(chat_id=-100123, verification_enabled=True))

        monkeypatch.setattr(
            verify_flow,
            "get_settings",
            lambda: SimpleNamespace(global_admin_id_set=set()),
        )
        monkeypatch.setattr(
            verify_flow,
            "_send_verify_invite_message",
            AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=-100123), message_id=77)),
        )
        monkeypatch.setattr(verify_flow, "_verify_timeout_task", AsyncMock())

        async def restrict_after_token(*args, **kwargs):
            async with session_scope() as session:
                tokens = (await session.execute(select(VerifyToken))).scalars().all()
                assert len(tokens) == 1
                assert tokens[0].used_at is None
                assert await guest_bots._has_pending_verification(session, -100123, 55)

        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(side_effect=restrict_after_token),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )
        await verify_flow._start_verification_for_member(
            bot,
            chat_id=-100123,
            user_id=55,
            chat_title="Test",
        )
        bot.restrict_chat_member.assert_awaited_once()
        async with session_scope() as session:
            tokens = (await session.execute(select(VerifyToken))).scalars().all()
            assert len(tokens) == 1
        await engine.dispose()

    asyncio.run(run())
