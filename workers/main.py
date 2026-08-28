from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import create_async_engine

from config import settings
from workers.competition_lifecycle import run_forever as run_competition_lifecycle
from workers.lock import acquire_worker_lock, release_worker_lock
from workers.price_poller import main as run_price_poller
from workers.tp_sl_engine import run_forever as run_tp_sl_engine

logger = logging.getLogger(__name__)


async def main() -> None:
    if settings.require_postgres and not settings.database_is_postgres:
        raise RuntimeError("REQUIRE_POSTGRES=true but DATABASE_URL is not PostgreSQL")
    engine = create_async_engine(settings.database_url_async, echo=False)
    lock_connection = None
    try:
        lock_connection = await acquire_worker_lock(engine)
        if lock_connection is None:
            logger.warning("Worker singleton lock is disabled for non-PostgreSQL local runtime")
        logger.info("Production worker started: price polling, TP/SL, competition lifecycle")
        await asyncio.gather(
            run_price_poller(engine),
            run_tp_sl_engine(engine),
            run_competition_lifecycle(engine),
        )
    finally:
        await release_worker_lock(lock_connection)
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
