from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger
from aiogram import Bot, Dispatcher

from anticheatbot.config import get_settings
from anticheatbot.db.session import configure_engine, session_scope
from anticheatbot.handlers import setup_handlers
from anticheatbot.handlers.verify_flow import restore_verification_timeouts, stop_verification_timeouts
from anticheatbot.services.groups import load_guest_bot_blocked_chats
from anticheatbot.web.app import create_http_app


class RequestAccessLogger(AbstractAccessLogger):
    def log(self, request: web.Request, response: web.StreamResponse, time: float) -> None:
        # Verification tokens are query parameters; never persist them in access logs.
        self.logger.info("%s %s %s %.3fs", request.method, request.path, response.status, time)


async def _async_main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    engine = configure_engine(settings.database_url)
    async with session_scope() as session:
        await load_guest_bot_blocked_chats(session)

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    setup_handlers(dp)

    app = create_http_app(bot=bot, settings=settings)
    runner = web.AppRunner(app, access_log_class=RequestAccessLogger)
    try:
        await runner.setup()
        site = web.TCPSite(runner, host=settings.http_host, port=settings.http_port)
        await site.start()
        logging.info("HTTP listening on %s:%s", settings.http_host, settings.http_port)
        restored = await restore_verification_timeouts(bot)
        logging.info("Restored %s pending verification timeouts", restored)
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        await stop_verification_timeouts()
        await runner.cleanup()
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(_async_main())
