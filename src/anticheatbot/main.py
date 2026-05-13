from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher

from anticheatbot.config import get_settings
from anticheatbot.db.session import configure_engine
from anticheatbot.handlers import setup_handlers
from anticheatbot.web.app import create_http_app


async def _async_main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    configure_engine(settings.database_url)

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    setup_handlers(dp)

    app = create_http_app(bot=bot, settings=settings)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.http_host, port=settings.http_port)
    await site.start()
    logging.info("HTTP listening on %s:%s", settings.http_host, settings.http_port)

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


def main() -> None:
    asyncio.run(_async_main())
