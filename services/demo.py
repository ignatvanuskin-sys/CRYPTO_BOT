from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.competition_models import Competition, CompetitionStatus, CompetitionParticipant
from db.models import User
from db.paper_models import PaperPosition
from services.competition import join_competition
from services.trading_account import get_or_create_trading_account

# The requested 1.43 display value for ranks 4-10 sums to 100.01. The last
# cent is corrected on rank 10 so the persisted awards never exceed $100.
DEMO_PRIZES = [
    Decimal("50.00"),
    Decimal("25.00"),
    Decimal("15.00"),
    Decimal("1.43"),
    Decimal("1.43"),
    Decimal("1.43"),
    Decimal("1.43"),
    Decimal("1.43"),
    Decimal("1.43"),
    Decimal("1.42"),
]


async def create_demo_cup(session: AsyncSession) -> Competition:
    result = await session.execute(
        select(Competition)
        .where(
            Competition.name == "DEMO TRADING CUP",
            Competition.status == CompetitionStatus.ACTIVE.value,
        )
        .order_by(Competition.id.desc())
        .limit(1)
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    # A fresh database is seeded with a placeholder weekly cup by migration
    # 003. Reuse that empty active cup for the admin demo instead of creating
    # two competing ACTIVE tournaments. Never mutate a cup with participants
    # or positions.
    active = (
        await session.execute(
            select(Competition)
            .where(Competition.status == CompetitionStatus.ACTIVE.value)
            .order_by(Competition.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if active is not None:
        participant_count = (
            await session.execute(
                select(func.count()).select_from(CompetitionParticipant).where(
                    CompetitionParticipant.competition_id == active.id
                )
            )
        ).scalar_one()
        position_count = (
            await session.execute(
                select(func.count()).select_from(PaperPosition).where(
                    PaperPosition.competition_id == active.id
                )
            )
        ).scalar_one()
        if participant_count or position_count:
            raise ValueError("Сначала завершите текущий турнир")
        now = datetime.now(timezone.utc)
        active.name = "DEMO TRADING CUP"
        active.starts_at = now
        active.ends_at = now + timedelta(hours=settings.demo_cup_duration_hours)
        active.initial_balance = Decimal(settings.initial_balance_usd)
        active.prize_pool = Decimal(settings.demo_prize_pool)
        active.ranking_metric = "ROI"
        await session.flush()
        return active

    now = datetime.now(timezone.utc)
    cup = Competition(
        name="DEMO TRADING CUP",
        status=CompetitionStatus.ACTIVE.value,
        starts_at=now,
        ends_at=now + timedelta(hours=settings.demo_cup_duration_hours),
        initial_balance=Decimal(settings.initial_balance_usd),
        prize_pool=Decimal(settings.demo_prize_pool),
        ranking_metric="ROI",
        price_source="BINGX",
        market_type="USD_M_PERPETUAL",
    )
    session.add(cup)
    await session.flush()
    return cup


async def seed_demo_players(session: AsyncSession, competition_id: int) -> int:
    if not settings.demo_seed_enabled:
        raise PermissionError("Demo player seeding is disabled")
    count = max(1, min(settings.demo_player_count, 50))
    created = 0
    for index in range(1, count + 1):
        telegram_id = -900000000000 - index
        username = f"DEMO_{index:02d}"
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                is_simulated=True,
            )
            session.add(user)
            await session.flush()
            created += 1
        elif not user.is_simulated:
            raise RuntimeError(f"Telegram ID collision for simulated user {telegram_id}")
        await get_or_create_trading_account(session, user.id)
        await join_competition(session, user.id, competition_id)
    return created
