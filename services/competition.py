from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from db.competition_models import Competition, CompetitionParticipant, CompetitionPrize, CompetitionStatus
from services.metrics import increment
from db.paper_models import TradingAccount
from services.trading_account import get_or_create_trading_account

async def get_active_competition(session: AsyncSession) -> Competition | None:
    result = await session.execute(
        select(Competition)
        .where(
            Competition.status == CompetitionStatus.ACTIVE.value,
            Competition.starts_at <= datetime.now(timezone.utc),
            Competition.ends_at > datetime.now(timezone.utc),
        )
        .order_by(Competition.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()

async def get_or_create_default_competition(session: AsyncSession) -> Competition:
    dialect = session.bind.dialect.name if session.bind else ""
    if dialect == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": 82463519})
    comp = await get_active_competition(session)
    if comp:
        return comp
    now = datetime.now(timezone.utc)
    comp = Competition(
        name="Weekly Trading Cup #1",
        status=CompetitionStatus.ACTIVE.value,
        starts_at=now,
        ends_at=now + timedelta(days=7),
        initial_balance=Decimal("10000"),
        prize_pool=Decimal("500"),
        ranking_metric="ROI",
        price_source="BINGX",
        market_type="USD_M_PERPETUAL",
    )
    session.add(comp)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        comp = await get_active_competition(session)
        if comp is None:
            raise
    return comp

async def join_competition(session: AsyncSession, user_id: int, competition_id: int | None = None) -> CompetitionParticipant:
    if competition_id is None:
        comp = await get_or_create_default_competition(session)
        competition_id = comp.id
    else:
        comp = await session.get(Competition, competition_id)
        if not comp:
            raise ValueError("Competition not found")
    if comp.status != CompetitionStatus.ACTIVE.value:
        raise ValueError("Competition is not active")
    if comp.ends_at <= datetime.now(timezone.utc):
        raise ValueError("Competition has ended")
    # check already joined
    result = await session.execute(select(CompetitionParticipant).where(CompetitionParticipant.competition_id == competition_id, CompetitionParticipant.user_id == user_id))
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    # get trading account to set starting equity
    acc = await get_or_create_trading_account(session, user_id)
    # starting equity = initial_balance (or account equity)
    starting = comp.initial_balance
    part = CompetitionParticipant(
        competition_id=competition_id,
        user_id=user_id,
        starting_equity=starting,
        current_equity=starting,
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        roi=Decimal("0"),
    )
    try:
        async with session.begin_nested():
            session.add(part)
            await session.flush()
    except IntegrityError:
        existing = (
            await session.execute(
                select(CompetitionParticipant).where(
                    CompetitionParticipant.competition_id == competition_id,
                    CompetitionParticipant.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing
    increment("competition_joined")
    return part

async def update_participant_equity(session: AsyncSession, user_id: int, competition_id: int):
    result = await session.execute(select(CompetitionParticipant).where(CompetitionParticipant.competition_id == competition_id, CompetitionParticipant.user_id == user_id))
    part = result.scalar_one_or_none()
    if not part:
        return
    # get account
    from sqlalchemy import select as sel
    acc_res = await session.execute(sel(TradingAccount).where(TradingAccount.user_id == user_id))
    acc = acc_res.scalar_one_or_none()
    if not acc:
        return
    # current_equity = cash + unrealized (from paper positions)
    from db.paper_models import PaperPosition, PositionStatus
    q = await session.execute(select(PaperPosition).where(PaperPosition.account_id == acc.id, PaperPosition.status == PositionStatus.OPEN.value))
    positions = q.scalars().all()
    unrealized = sum((p.unrealized_pnl for p in positions), Decimal("0"))
    # also need to update via pricing? For now use stored unrealized
    part.unrealized_pnl = unrealized
    part.realized_pnl = acc.realized_pnl
    part.current_equity = (acc.cash_balance + acc.margin_used + unrealized).quantize(Decimal("0.01"))
    # ROI
    if part.starting_equity != 0:
        part.roi = ((part.current_equity - part.starting_equity) / part.starting_equity * 100).quantize(Decimal("0.0001"))
    else:
        part.roi = Decimal("0")
    await session.flush()

async def finish_competition(session: AsyncSession, competition_id: int):
    """Finalize once, freeze authoritative ranking and assign demo prizes."""
    comp = await session.get(Competition, competition_id, with_for_update=True)
    if not comp or comp.status == CompetitionStatus.FINISHED.value:
        return
    comp.status = CompetitionStatus.FINISHED.value
    from services.leaderboard import build_leaderboard, snapshot_leaderboard

    leaderboard = await build_leaderboard(session, competition_id)
    # snapshot_leaderboard is guarded by the database uniqueness constraint;
    # only create it when no snapshot exists for this competition.
    from db.competition_models import LeaderboardSnapshot
    snapshot_exists = await session.execute(
        select(LeaderboardSnapshot.id)
        .where(LeaderboardSnapshot.competition_id == competition_id)
        .limit(1)
    )
    if snapshot_exists.scalar_one_or_none() is None:
        await snapshot_leaderboard(session, competition_id, leaderboard)

    if comp.name == "DEMO TRADING CUP":
        from services.demo import DEMO_PRIZES

        prizes_exist = await session.execute(
            select(CompetitionPrize.id)
            .where(CompetitionPrize.competition_id == competition_id)
            .limit(1)
        )
        if prizes_exist.scalar_one_or_none() is None:
            now = datetime.now(timezone.utc)
            for entry, amount in zip(leaderboard[: len(DEMO_PRIZES)], DEMO_PRIZES):
                session.add(
                    CompetitionPrize(
                        competition_id=competition_id,
                        rank=entry["rank"],
                        user_id=entry["user_id"],
                        amount=amount,
                        status="ASSIGNED",
                        assigned_at=now,
                    )
                )
    increment("competition_finished")
    await session.flush()
