import asyncio
import logging
from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from config import settings
from bot.handlers.user import router as user_router
from bot.handlers.admin import router as admin_router

async def main():
    logging.basicConfig(level=logging.INFO)
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN not set")
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()

    @dp.update.outer_middleware
    async def db_middleware(handler, event, data):
        async with session_factory() as session:
            data["session"] = session
            return await handler(event, data)

    dp.include_router(user_router)
    dp.include_router(admin_router)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
