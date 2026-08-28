from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.handlers.admin import router as admin_router
from bot.handlers.leaderboard import router as leaderboard_router
from bot.handlers.profile import router as profile_router
from bot.handlers.trade import router as trade_router
from bot.handlers.user import router as user_router
from config import settings
from workers.lock import BOT_LOCK_KEY, acquire_advisory_lock, release_advisory_lock

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN not set")
    if settings.require_postgres and not settings.database_is_postgres:
        raise RuntimeError("REQUIRE_POSTGRES=true but DATABASE_URL is not PostgreSQL")
    if not settings.database_is_postgres:
        logger.warning("Telegram bot is running without PostgreSQL singleton lock")

    engine = create_async_engine(settings.database_url_async, echo=False)
    bot_lock = None
    bot = Bot(token=settings.bot_token)
    try:
        bot_lock = await acquire_advisory_lock(engine, BOT_LOCK_KEY, "Telegram bot")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        dp = Dispatcher()

        @dp.update.outer_middleware
        async def db_middleware(handler, event, data):
            async with session_factory() as session:
                data["session"] = session
                return await handler(event, data)

        if settings.trading_mode == "paper":
            # Paper bot owns the modern competition/trading flow. It never
            # registers legacy handlers that execute from process-local cache.
            dp.include_router(profile_router)
            dp.include_router(trade_router)
            dp.include_router(leaderboard_router)
        else:
            # Legacy weekly mode is isolated from the modern paper routers.
            dp.include_router(user_router)
        dp.include_router(admin_router)

        logger.info("Telegram polling starting for configured bot")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await release_advisory_lock(bot_lock, BOT_LOCK_KEY)
        await bot.session.close()
        await engine.dispose()
        logger.info("Telegram polling stopped")


if __name__ == "__main__":
    asyncio.run(main())
