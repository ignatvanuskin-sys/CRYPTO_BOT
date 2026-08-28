from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from config import settings
from db.competition_models import Competition, CompetitionStatus
from db.paper_models import PaperPosition, PositionStatus, TradingAccount
from services.competition import finish_competition
from services.paper_adapter import PaperError, close_position
from services.notifications import notify_competition_finished

logger = logging.getLogger(__name__)


async def finalize_competition_session(session, competition_id: int) -> bool:
    competition = await session.get(Competition, competition_id, with_for_update=True)
    if not competition or competition.status != CompetitionStatus.ACTIVE.value:
        return False
    positions_result = await session.execute(
        select(PaperPosition).where(
            PaperPosition.competition_id == competition.id,
            PaperPosition.status == PositionStatus.OPEN.value,
        )
    )
    for position in positions_result.scalars().all():
        account = await session.get(TradingAccount, position.account_id)
        if not account:
            raise RuntimeError(f"Account missing for position {position.id}")
        # A position may have been closed concurrently by TP/SL or a manual
        # close between the snapshot query and here; skip it so finalization
        # still completes and stays idempotent.
        if position.status != PositionStatus.OPEN.value:
            continue
        try:
            await close_position(
                session,
                position,
                account,
                idempotency_key=f"competition_end:{competition.id}:{position.id}",
                reason="manual",
            )
        except PaperError:
            await session.refresh(position)
            if position.status != PositionStatus.OPEN.value:
                continue
            raise
    await finish_competition(session, competition.id)
    return True


async def finalize_expired_competitions(engine: AsyncEngine) -> int:
    """Close paper positions and finalize expired competitions once."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    finalized = 0
    finalized_ids: list[int] = []
    async with factory() as session:
        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(Competition)
            .where(
                Competition.status == CompetitionStatus.ACTIVE.value,
                Competition.ends_at <= now,
            )
            .with_for_update()
        )
        competitions = result.scalars().all()
        for competition in competitions:
            try:
                async with session.begin_nested():
                    if await finalize_competition_session(session, competition.id):
                        finalized += 1
                        finalized_ids.append(competition.id)
            except Exception:
                logger.exception("Finalize failed for competition %s", competition.id)
                continue
        if finalized:
            await session.commit()
    for competition_id in finalized_ids:
        await notify_competition_finished(engine, competition_id)
    return finalized


async def run_forever(engine: AsyncEngine | None = None) -> None:
    owns_engine = engine is None
    if engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(settings.database_url_async, echo=False)
    try:
        while True:
            try:
                count = await finalize_expired_competitions(engine)
                if count:
                    logger.info("Finalized %s expired paper competitions", count)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Competition lifecycle pass failed; leaving competitions unchanged")
            await asyncio.sleep(10)
    finally:
        if owns_engine:
            await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())
