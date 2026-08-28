"""Shared market-data + lifecycle integrity tests (aiosqlite).

These prove the modern paper trading path reads the canonical market_snapshots
table and never silently executes from a process-local price cache. Real
PostgreSQL FOR UPDATE / race behaviour is covered by tests/test_paper_race_pg.py
and tests/test_race_pg.py (skipped without Docker).
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
from db.market_data import MarketSnapshot
from db.models import User
from db.paper_models import Instrument, PaperPosition, TradingAccount
from services.bingx_market_data import (
    MarketDataInvalid,
    MarketDataStale,
    PriceSnapshot,
    get_execution_snapshot,
    persist_snapshot,
    validate_snapshot,
)
from services.competition import finish_competition, join_competition
from services.paper_adapter import PaperError, close_position, open_position
from services.pricing import price_cache
from services.trading_account import get_or_create_trading_account
from workers.competition_lifecycle import finalize_competition_session

pytestmark = pytest.mark.asyncio


async def _setup_paper(session, name="TEST CUP", ends_in_hours=24):
    now = datetime.now(timezone.utc)
    session.add(
        Instrument(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            status="active",
            min_quantity=Decimal("0.000001"),
        )
    )
    competition = Competition(
        name=name,
        status=CompetitionStatus.ACTIVE.value,
        starts_at=now,
        ends_at=now + timedelta(hours=ends_in_hours),
        initial_balance=Decimal("10000"),
        prize_pool=Decimal("100"),
        ranking_metric="ROI",
        price_source="BINGX",
        market_type="USD_M_PERPETUAL",
    )
    user = User(telegram_id=700001, username="shared-test")
    session.add_all([competition, user])
    await session.flush()
    account = await get_or_create_trading_account(session, user.id)
    await join_competition(session, user.id, competition.id)
    return account, competition, now


async def test_shared_snapshot_is_source_of_truth_not_local_cache(session):
    account, competition, now = await _setup_paper(session)
    # Canonical shared snapshot persisted in the DB.
    await persist_snapshot(
        session,
        PriceSnapshot("BTCUSDT", Decimal("50000"), Decimal("50010"), Decimal("50005"), now, now),
    )
    # A different (wrong) value sits in the process-local cache — must be ignored.
    price_cache.update("BTCUSDT", Decimal("99999"), now)
    await session.commit()

    position = await open_position(
        session,
        account,
        "BTCUSDT",
        "LONG",
        notional=Decimal("500"),
        competition_id=competition.id,
        idempotency_key="src-open",
    )
    await session.commit()
    # LONG OPEN must use the DB ask (50010), NOT the local-cache value (99999).
    assert position.entry_price == Decimal("50010.000000000000")


async def test_no_execution_without_shared_snapshot(session):
    account, competition, now = await _setup_paper(session)
    # Neither DB snapshot nor local cache contains BTCUSDT.
    price_cache.clear()
    await session.commit()
    with pytest.raises(PaperError):
        await open_position(
            session,
            account,
            "BTCUSDT",
            "LONG",
            notional=Decimal("500"),
            competition_id=competition.id,
            idempotency_key="no-data-open",
        )


async def test_stale_shared_snapshot_rejected(session):
    account, competition, now = await _setup_paper(session)
    await persist_snapshot(
        session,
        PriceSnapshot("BTCUSDT", Decimal("50000"), Decimal("50010"), Decimal("50005"), now - timedelta(seconds=30), now - timedelta(seconds=30)),
    )
    await session.commit()
    with pytest.raises(MarketDataStale):
        await get_execution_snapshot(session, "BTCUSDT", 2000)


async def test_future_shared_snapshot_rejected(session):
    account, competition, now = await _setup_paper(session)
    # Insert a snapshot whose exchange timestamp is unambiguously in the future.
    # persist_snapshot validates at WRITE and rejects garbage, so we insert the
    # row directly to exercise the READ-path guard in get_execution_snapshot.
    future = now + timedelta(days=1)
    session.add(
        MarketSnapshot(
            symbol="BTCUSDT",
            source="BINGX",
            market_type="PERPETUAL",
            bid=Decimal("50000"),
            ask=Decimal("50010"),
            last=Decimal("50005"),
            exchange_timestamp=future,
            received_at=now,
            updated_at=now,
        )
    )
    await session.commit()
    session.expire_all()
    with pytest.raises(MarketDataInvalid):
        await get_execution_snapshot(session, "BTCUSDT", 2000)


async def test_validate_snapshot_rejects_bad_values():
    now = datetime.now(timezone.utc)
    mk = lambda b, a, l: PriceSnapshot("BTCUSDT", Decimal(str(b)), Decimal(str(a)), Decimal(str(l)), now, now)
    with pytest.raises(MarketDataInvalid):
        validate_snapshot(mk(0, 1, 1))            # zero bid
    with pytest.raises(MarketDataInvalid):
        validate_snapshot(mk(2, 1, 1))            # ask < bid
    with pytest.raises(MarketDataInvalid):
        validate_snapshot(mk(-1, 1, 1))           # negative bid
    with pytest.raises(MarketDataInvalid):
        validate_snapshot(mk(1, 1, 0))            # zero last
    with pytest.raises(MarketDataInvalid):
        validate_snapshot(PriceSnapshot("BTCUSDT", Decimal("1"), Decimal("1"), Decimal("1"), None, now))  # missing ts
    with pytest.raises(MarketDataInvalid):
        validate_snapshot(PriceSnapshot("BTCUSDT", Decimal("nan"), Decimal("1"), Decimal("1"), now, now))  # NaN
    with pytest.raises(MarketDataInvalid):
        validate_snapshot(PriceSnapshot("BTCUSDT", Decimal("inf"), Decimal("1"), Decimal("1"), now, now))  # Infinity


async def test_open_rejected_before_competition_starts(session):
    account, competition, now = await _setup_paper(session, ends_in_hours=25)
    # Mark the cup as not yet started.
    competition.starts_at = now + timedelta(hours=1)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("50000"), Decimal("50010"), Decimal("50005"), now, now))
    await session.commit()
    with pytest.raises(PaperError, match="Competition ended"):
        await open_position(
            session,
            account,
            "BTCUSDT",
            "LONG",
            notional=Decimal("500"),
            competition_id=competition.id,
            idempotency_key="before-start",
        )


async def test_open_rejected_after_competition_end(session):
    # Set up as ACTIVE (so join succeeds), then move the end into the past.
    account, competition, now = await _setup_paper(session, ends_in_hours=25)
    competition.ends_at = now - timedelta(hours=1)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("50000"), Decimal("50010"), Decimal("50005"), now, now))
    await session.commit()
    with pytest.raises(PaperError, match="Competition ended"):
        await open_position(
            session,
            account,
            "BTCUSDT",
            "LONG",
            notional=Decimal("500"),
            competition_id=competition.id,
            idempotency_key="after-end",
        )


async def test_finalize_skips_already_closed_position(session):
    account, competition, now = await _setup_paper(session, name="DEMO TRADING CUP")
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("50000"), Decimal("50010"), Decimal("50005"), now, now))

    p1 = await open_position(session, account, "BTCUSDT", "LONG", notional=Decimal("500"), competition_id=competition.id, idempotency_key="fz-1")
    p2 = await open_position(session, account, "BTCUSDT", "SHORT", notional=Decimal("500"), competition_id=competition.id, idempotency_key="fz-2")
    await session.commit()
    # Manually close p1 (simulating a prior manual/TP-SL close).
    await close_position(session, p1, account, idempotency_key="fz-close-1")
    await session.commit()

    finished = await finalize_competition_session(session, competition.id)
    await session.commit()
    assert finished is True

    p1r = await session.get(PaperPosition, p1.id)
    p2r = await session.get(PaperPosition, p2.id)
    assert p1r.status == "CLOSED"
    assert p2r.status == "CLOSED"

    snap_cnt = (
        await session.execute(select(func.count()).select_from(LeaderboardSnapshot).where(LeaderboardSnapshot.competition_id == competition.id))
    ).scalar_one()
    assert snap_cnt == 1
    prize_cnt = (
        await session.execute(select(func.count()).select_from(CompetitionPrize).where(CompetitionPrize.competition_id == competition.id))
    ).scalar_one()
    assert prize_cnt == 1

    # A second finalization must be a no-op.
    finished2 = await finalize_competition_session(session, competition.id)
    await session.commit()
    assert finished2 is False
    snap_cnt2 = (
        await session.execute(select(func.count()).select_from(LeaderboardSnapshot).where(LeaderboardSnapshot.competition_id == competition.id))
    ).scalar_one()
    assert snap_cnt2 == snap_cnt
