from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import os

from aiohttp import web

from bot.handlers.admin import router as admin_router
from bot.handlers.leaderboard import router as leaderboard_router
from bot.handlers.profile import router as profile_router
from bot.handlers.trade import router as trade_router
from bot.middlewares.throttling import ThrottlingMiddleware
from config import settings
from workers.competition_lifecycle import run_forever as run_competition_lifecycle
from workers.lock import LOCK_KEY, acquire_advisory_lock, release_advisory_lock
from workers.price_poller import run_forever as run_price_poller
from workers.tp_sl_engine import run_forever as run_tp_sl_engine

logger = logging.getLogger(__name__)


async def _healthcheck_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "bot": "CRYPTO_BOT"})


async def _metrics_handler(request: web.Request) -> web.Response:
    from services.metrics import snapshot

    return web.json_response(snapshot())


async def _run_healthcheck_app() -> None:
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app.router.add_get("/health", _healthcheck_handler)
    app.router.add_get("/metrics", _metrics_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Healthcheck listening on :%s /health", port)
    # Keep running until cancelled
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await runner.cleanup()
        raise


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN not set")
    if settings.require_postgres and not settings.database_is_postgres:
        raise RuntimeError("REQUIRE_POSTGRES=true but DATABASE_URL is not PostgreSQL")
    if not settings.database_is_postgres:
        logger.warning("Bot is running without PostgreSQL singleton lock (local dev)")

    engine = create_async_engine(settings.database_url_async, echo=False)
    lock_connection = None
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    background_tasks: list[asyncio.Task] = []
    try:
        # Retry lock acquisition during rolling deploys (old container still holds lock)
        for attempt in range(15):
            try:
                lock_connection = await acquire_advisory_lock(engine, LOCK_KEY, "bot (single process)")
                break
            except RuntimeError as exc:
                if attempt == 14:
                    raise
                logger.warning("Singleton lock held, retry %s/15: %s", attempt + 1, exc)
                await asyncio.sleep(2)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        dp = Dispatcher()

        @dp.update.outer_middleware
        async def db_middleware(handler, event, data):
            async with session_factory() as session:
                data["session"] = session
                try:
                    return await handler(event, data)
                except Exception:
                    try:
                        await session.rollback()
                    except Exception:
                        pass
                    raise

        # Throttling: 0.8s per message, 0.3s per callback per user
        dp.message.middleware(ThrottlingMiddleware(message_rate=0.8, callback_rate=0.3))
        dp.callback_query.middleware(ThrottlingMiddleware(message_rate=0.8, callback_rate=0.3))

        dp.include_router(profile_router)
        dp.include_router(leaderboard_router)
        dp.include_router(trade_router)
        dp.include_router(admin_router)

        # Background tasks inside the SAME process and event loop:
        # price poller, TP/SL engine, competition lifecycle.
        background_tasks = [
            asyncio.create_task(run_price_poller(engine), name="price_poller"),
            asyncio.create_task(run_tp_sl_engine(engine), name="tp_sl_engine"),
            asyncio.create_task(run_competition_lifecycle(engine), name="competition_lifecycle"),
            asyncio.create_task(_run_healthcheck_app(), name="healthcheck"),
        ]

        logger.info("Single-process bot starting: polling + price poller + TP/SL + competition lifecycle")
        polling_task = asyncio.create_task(
            dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        )
        done, pending = await asyncio.wait(
            [polling_task, *background_tasks],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc:
                logger.error("Task %s crashed: %s", task.get_name(), exc)
                raise exc
    finally:
        for task in background_tasks:
            task.cancel()
        for task in background_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await release_advisory_lock(lock_connection, LOCK_KEY)
        await bot.session.close()
        await engine.dispose()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
