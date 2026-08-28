"""Real PostgreSQL paper-trading race tests.

These tests are skipped by the fixture when TEST_DATABASE_URL/Docker is absent.
They must not be replaced by SQLite tests: account locks and unique-key races
are PostgreSQL behavior.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.competition_models import Competition, CompetitionParticipant, LeaderboardSnapshot
from db.models import User
from db.paper_models import Instrument, PaperOrder, PaperPosition, TradingAccount
from services.bingx_market_data import MarketDataUnavailable, PriceSnapshot, get_execution_snapshot, persist_snapshot
from services.pricing import price_cache
from services.competition import finish_competition, join_competition
from services.paper_adapter import PaperError, close_position, open_position
from services.trading_account import get_or_create_trading_account

pytestmark = pytest.mark.asyncio


async def setup_paper(pg_engine):
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with factory() as session:
        now = datetime.now(timezone.utc)
        competition = Competition(
            name="RACE CUP",
            status="ACTIVE",
            starts_at=now,
            ends_at=now + timedelta(hours=24),
            initial_balance=Decimal("10000"),
            prize_pool=Decimal("100"),
            ranking_metric="ROI",
            price_source="BINGX",
            market_type="USD_M_PERPETUAL",
        )
        user = User(telegram_id=880001, username="race")
        session.add_all([
            competition,
            user,
            Instrument(
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                status="active",
                min_quantity=Decimal("0.000001"),
            ),
        ])
        await session.flush()
        account = await get_or_create_trading_account(session, user.id)
        await join_competition(session, user.id, competition.id)
        await persist_snapshot(
            session,
            PriceSnapshot(
                "BTCUSDT",
                Decimal("50000"),
                Decimal("50010"),
                Decimal("50005"),
                now,
                now,
            ),
        )
        await session.commit()
        return competition.id, user.id, account.id


async def test_postgres_execution_never_falls_back_to_local_cache(pg_engine):
    await setup_paper(pg_engine)
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    price_cache.update("ETHUSDT", Decimal("3000"), datetime.now(timezone.utc))
    async with factory() as session:
        with pytest.raises(MarketDataUnavailable):
            await get_execution_snapshot(session, "ETHUSDT", 2000)


async def test_same_open_key_is_one_position(pg_engine):
    competition_id, user_id, account_id = await setup_paper(pg_engine)
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)

    async def attempt():
        async with factory() as session:
            account = await session.get(TradingAccount, account_id)
            try:
                position = await open_position(
                    session,
                    account,
                    "BTCUSDT",
                    "LONG",
                    notional=Decimal("500"),
                    competition_id=competition_id,
                    idempotency_key="race-open-same-key",
                )
                await session.commit()
                return position.id
            except Exception:
                await session.rollback()
                raise

    first, second = await asyncio.gather(attempt(), attempt())
    assert first == second
    async with factory() as session:
        assert (await session.execute(select(func.count()).select_from(PaperPosition))).scalar_one() == 1
        assert (await session.execute(select(func.count()).select_from(PaperOrder))).scalar_one() == 1


async def test_manual_close_race_has_one_close(pg_engine):
    competition_id, user_id, account_id = await setup_paper(pg_engine)
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with factory() as session:
        account = await session.get(TradingAccount, account_id)
        position = await open_position(
            session,
            account,
            "BTCUSDT",
            "LONG",
            notional=Decimal("500"),
            competition_id=competition_id,
            idempotency_key="race-close-open",
        )
        await session.commit()
        position_id = position.id

    async def attempt(key):
        async with factory() as session:
            account = await session.get(TradingAccount, account_id)
            position = await session.get(PaperPosition, position_id)
            try:
                result = await close_position(session, position, account, idempotency_key=key)
                await session.commit()
                return "ok", result[0].id
            except PaperError:
                await session.rollback()
                return "closed_elsewhere", position_id

    results = await asyncio.gather(attempt("race-close-a"), attempt("race-close-b"))
    assert sum(result[0] == "ok" for result in results) == 1
    async with factory() as session:
        close_count = await session.execute(
            select(func.count()).select_from(PaperOrder).where(PaperOrder.reduce_only.is_(True))
        )
        assert close_count.scalar_one() == 1


async def test_two_finalizers_create_one_snapshot(pg_engine):
    competition_id, user_id, account_id = await setup_paper(pg_engine)
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    async def finalize():
        async with factory() as session:
            try:
                await finish_competition(session, competition_id)
                await session.commit()
                return "ok"
            except Exception:
                await session.rollback()
                return "error"

    results = await asyncio.gather(finalize(), finalize())
    assert results.count("ok") >= 1
    async with factory() as session:
        count = await session.execute(
            select(func.count()).select_from(LeaderboardSnapshot).where(
                LeaderboardSnapshot.competition_id == competition_id
            )
        )
        assert count.scalar_one() == 1
