import asyncio
import logging
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from config import settings
from db.models import Week
from services.weekly_cycle import close_week, get_or_create_active_week
from sqlalchemy import select
from decimal import Decimal

logger = logging.getLogger(__name__)

async def run_weekly_close():
    engine = create_async_engine(settings.database_url_async, echo=False)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        async with session.begin():
            week = await get_or_create_active_week(session)
            # only close if week has ended? For scheduler we close regardless
            await close_week(session, week, prize_top_n=settings.prize_top_n, grant_amount=Decimal(settings.weekly_grant_amount))
            await session.commit()
    logger.info("Weekly close completed")

def start_scheduler():
    scheduler = AsyncIOScheduler(timezone=settings.week_reset_tz)
    # parse WEEK_RESET_TIME e.g. 00:00
    hour, minute = map(int, settings.week_reset_time.split(":"))
    day_map = {"monday":"mon","tuesday":"tue","wednesday":"wed","thursday":"thu","friday":"fri","saturday":"sat","sunday":"sun"}
    day_of_week = day_map.get(settings.week_reset_day.lower(), "mon")
    scheduler.add_job(run_weekly_close, CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute))
    scheduler.start()
    return scheduler

async def main():
    logging.basicConfig(level=logging.INFO)
    scheduler = start_scheduler()
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())

