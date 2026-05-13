from __future__ import annotations

from aiogram import Dispatcher

from anticheatbot.handlers import admin_cmds, moderation, root, verify_flow


def setup_handlers(dp: Dispatcher) -> None:
    dp.include_router(root.router)
    dp.include_router(admin_cmds.router)
    dp.include_router(verify_flow.router)
    dp.include_router(moderation.router)
