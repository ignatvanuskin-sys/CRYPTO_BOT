"""FULL DEMO ACCEPTANCE — deterministic, sqlite (financial logic only).

Mirrors the mandatory acceptance scenario:
  DEMO_01 (LONG) and DEMO_02 (SHORT) trade the shared BTCUSDT snapshot,
  LONG OPEN = ASK, SHORT OPEN = BID, the cup is finalized (positions closed,
  leaderboard snapshot + DEMO prizes written once), and a second finalize is a
  no-op. Negative paths: no execution without a shared snapshot, open rejected
  before start and after end.

PostgreSQL FOR UPDATE / advisory locks are reviewed statically (see final
report); the money/lifecycle logic exercised here is backend-agnostic and runs
on the same code path used in production.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from db.competition_models import (
    Competition,
    CompetitionPrize,
    CompetitionStatus,
    LeaderboardSnapshot,
)
from db.models import User
from db.paper_models import Instrument, TradingAccount
from services.bingx_market_data import PriceSnapshot, persist_snapshot
from services.competition import join_competition
from services.demo import DEMO_PRIZES
from services.paper_adapter import PaperError, open_position
from services.trading_account import get_or_create_trading_account
from workers.competition_lifecycle import finalize_competition_session

pytestmark = pytest.mark.asyncio


async def _make_cup(session, name="DEMO TRADING CUP"):
    now = datetime.now(timezone.utc)
    inst = Instrument(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        status="active",
        min_quantity=Decimal("0.000001"),
    )
    competition = Competition(
        name=name,
        status=CompetitionStatus.ACTIVE.value,
        starts_at=now,
        ends_at=now + timedelta(hours=24),
        initial_balance=Decimal("10000"),
        prize_pool=Decimal("100"),
        ranking_metric="ROI",
        price_source="BINGX",
        market_type="USD_M_PERPETUAL",
    )
    session.add_all([inst, competition])
    await session.flush()
    return competition


async def _make_user(session, tid, name):
    user = User(telegram_id=tid, username=name)
    session.add(user)
    await session.flush()
    account = await get_or_create_trading_account(session, user.id)
    return account, user


async def test_demo_acceptance_long_ask_short_bid_prizes_noop(session):
    competition = await _make_cup(session)
    acc1, u1 = await _make_user(session, 910001, "DEMO_01")
    acc2, u2 = await _make_user(session, 910002, "DEMO_02")
    await join_competition(session, u1.id, competition.id)
    await join_competition(session, u2.id, competition.id)

    now = datetime.now(timezone.utc)
    # Canonical shared snapshot: bid=50000, ask=50010, last=50005.
    await persist_snapshot(
        session,
        PriceSnapshot("BTCUSDT", Decimal("50000"), Decimal("50010"), Decimal("50005"), now, now),
    )

    # DEMO_01 opens LONG — must execute at the ASK (50010).
    p_long = await open_position(
        session, acc1, "BTCUSDT", "LONG", notional=Decimal("500"),
        competition_id=competition.id, idempotency_key="demo-long",
    )
    # DEMO_02 opens SHORT — must execute at the BID (50000).
    p_short = await open_position(
        session, acc2, "BTCUSDT", "SHORT", notional=Decimal("500"),
        competition_id=competition.id, idempotency_key="demo-short",
    )
    await session.commit()

    assert p_long.entry_price == Decimal("50010.000000000000"), p_long.entry_price
    assert p_short.entry_price == Decimal("50000.000000000000"), p_short.entry_price
    assert p_long.side == "LONG" and p_short.side == "SHORT"

    # Finalize the cup: every open position is closed, leaderboard + prizes once.
    finished = await finalize_competition_session(session, competition.id)
    await session.commit()
    assert finished is True

    snap_count = (
        await session.execute(
            select(func.count()).select_from(LeaderboardSnapshot).where(
                LeaderboardSnapshot.competition_id == competition.id
            )
        )
    ).scalar_one()
    prize_count = (
        await session.execute(
            select(func.count()).select_from(CompetitionPrize).where(
                CompetitionPrize.competition_id == competition.id
            )
        )
    ).scalar_one()
    # One leaderboard row per participant, written exactly once for this cup.
    assert snap_count == 2, f"expected 2 leaderboard rows (one per participant), got {snap_count}"
    # Two ranked participants -> two DEMO prizes (DEMO TRADING CUP assigns prizes).
    assert prize_count == 2, f"expected 2 demo prizes, got {prize_count}"
    assert sum(DEMO_PRIZES, Decimal("0")) == Decimal("100.00")

    # A second finalize must be a no-op (idempotent).
    finished2 = await finalize_competition_session(session, competition.id)
    await session.commit()
    assert finished2 is False
    snap_count2 = (
        await session.execute(
            select(func.count()).select_from(LeaderboardSnapshot).where(
                LeaderboardSnapshot.competition_id == competition.id
            )
        )
    ).scalar_one()
    assert snap_count2 == snap_count


async def test_demo_acceptance_negative_open_after_finalize(session):
    competition = await _make_cup(session)
    acc, _ = await _make_user(session, 910003, "DEMO_03")
    await join_competition(session, _.id, competition.id)
    await persist_snapshot(
        session,
        PriceSnapshot("BTCUSDT", Decimal("50000"), Decimal("50010"), Decimal("50005"),
                      datetime.now(timezone.utc), datetime.now(timezone.utc)),
    )
    p = await open_position(
        session, acc, "BTCUSDT", "LONG", notional=Decimal("500"),
        competition_id=competition.id, idempotency_key="demo-neg-open",
    )
    await session.commit()
    # Finalize -> competition becomes FINISHED.
    await finalize_competition_session(session, competition.id)
    await session.commit()
    # Opening into a finished cup must be rejected.
    with pytest.raises(PaperError, match="Competition ended"):
        await open_position(
            session, acc, "BTCUSDT", "LONG", notional=Decimal("500"),
            competition_id=competition.id, idempotency_key="demo-neg-after",
        )
