"""Phase 1 FIX #3 concurrency tests — PostgreSQL only.

These prove idempotency + ledger/account atomicity under REAL concurrency
(FOR UPDATE / READ COMMITTED). Skipped when PG (TEST_DATABASE_URL /
DATABASE_URL / testcontainers) is unavailable.

Do NOT weaken these tests to make SQLite pass — concurrency requires PG.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.competition_models import Competition, CompetitionStatus, Execution
from db.models import User
from db.paper_models import (
    AccountLedger,
    Instrument,
    LedgerType,
    PaperOrder,
    PaperPosition,
    PositionStatus,
    TradingAccount,
)
from services.bingx_market_data import PriceSnapshot, persist_snapshot
from services.competition import join_competition
from services.paper_adapter import close_position, open_position
from services.trading_account import get_or_create_trading_account

pytestmark = [pytest.mark.asyncio, pytest.mark.pg]


async def _setup(pg_engine, telegram_id=600001):
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with factory() as session:
        now = datetime.now(timezone.utc)
        session.add(
            Instrument(
                symbol="SOLUSDT",
                base_asset="SOL",
                quote_asset="USDT",
                status="active",
                price_precision=3,
                quantity_precision=6,
                min_quantity=Decimal("0.000001"),
                max_leverage=300,
            )
        )
        comp = Competition(
            name="PH1 PG",
            status=CompetitionStatus.ACTIVE.value,
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(hours=24),
            initial_balance=Decimal("10000"),
            prize_pool=Decimal("0"),
            ranking_metric="ROI",
            price_source="BINGX",
            market_type="USD_M_PERPETUAL",
        )
        user = User(telegram_id=telegram_id, username=f"pg_ph1_{telegram_id}")
        session.add_all([comp, user])
        await session.flush()
        account = await get_or_create_trading_account(session, user.id)
        await join_competition(session, user.id, comp.id)
        await persist_snapshot(
            session,
            PriceSnapshot("SOLUSDT", Decimal("100.00"), Decimal("100.10"), Decimal("100.05"), now, now),
        )
        await session.commit()
        return user.id, account.id, comp.id


async def _assert_pg_reconciliation(pg_engine, account_id):
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with factory() as session:
        acc = await session.get(TradingAccount, account_id)
        ledger_sum = (await session.execute(
            select(func.coalesce(func.sum(AccountLedger.amount), 0)).where(AccountLedger.account_id == account_id)
        )).scalar_one()
        ledger_sum = Decimal(str(ledger_sum))
        assert acc.cash_balance.quantize(Decimal("0.01")) == ledger_sum.quantize(Decimal("0.01"))
        assert acc.cash_balance >= 0


async def test_pg_concurrent_open_same_key_single_effect(pg_engine):
    """FIX #3: two concurrent OPEN with same idempotency key produce exactly
    one position, one execution, one TRADE_OPEN ledger, one balance mutation."""
    user_id, account_id, comp_id = await _setup(pg_engine)
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)

    async def attempt():
        async with factory() as session:
            account = await session.get(TradingAccount, account_id)
            try:
                pos = await open_position(
                    session,
                    account,
                    "SOLUSDT",
                    "LONG",
                    notional=Decimal("500"),
                    competition_id=comp_id,
                    idempotency_key="pg-idem-open",
                    leverage=10,
                )
                await session.commit()
                return pos.id
            except Exception as e:
                await session.rollback()
                return f"ERR:{type(e).__name__}:{e}"

    results = await asyncio.gather(attempt(), attempt())
    # exactly one real position id (the other is either the same id or an idempotent hit)
    ids = [r for r in results if isinstance(r, int)]
    assert len(ids) == 1 or (len(ids) == 2 and ids[0] == ids[1]), f"unexpected: {results}"
    pos_id = ids[0]

    async with factory() as session:
        pos_count = (await session.execute(select(func.count()).select_from(PaperPosition))).scalar_one()
        order_count = (await session.execute(select(func.count()).select_from(PaperOrder))).scalar_one()
        open_ledger_count = (await session.execute(
            select(func.count()).select_from(AccountLedger).where(
                AccountLedger.account_id == account_id,
                AccountLedger.type == LedgerType.TRADE_OPEN.value,
            )
        )).scalar_one()
        assert pos_count == 1, f"positions={pos_count}"
        assert order_count == 1, f"orders={order_count}"
        assert open_ledger_count == 1, f"TRADE_OPEN ledgers={open_ledger_count}"
        # margin 500/10 = 50 deducted once
        acc = await session.get(TradingAccount, account_id)
        assert acc.cash_balance == Decimal("9950.00")
    await _assert_pg_reconciliation(pg_engine, account_id)


async def test_pg_concurrent_close_same_key_single_effect(pg_engine):
    """FIX #3: two concurrent CLOSE with same key produce exactly one close."""
    user_id, account_id, comp_id = await _setup(pg_engine, telegram_id=600002)
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with factory() as session:
        account = await session.get(TradingAccount, account_id)
        pos = await open_position(
            session, account, "SOLUSDT", "LONG",
            notional=Decimal("500"), competition_id=comp_id,
            idempotency_key="pg-idem-close-open", leverage=10,
        )
        await session.commit()
        pos_id = pos.id
    # move price
    later = datetime.now(timezone.utc)
    async with factory() as session:
        await persist_snapshot(
            session,
            PriceSnapshot("SOLUSDT", Decimal("101.00"), Decimal("101.10"), Decimal("101.05"), later, later),
        )
        await session.commit()

    async def close_attempt():
        async with factory() as session:
            account = await session.get(TradingAccount, account_id)
            position = await session.get(PaperPosition, pos_id)
            try:
                closed, pnl = await close_position(
                    session, position, account,
                    idempotency_key="pg-idem-close", reason="manual",
                )
                await session.commit()
                return ("ok", pnl)
            except Exception as e:
                await session.rollback()
                return ("err", f"{type(e).__name__}:{e}")

    results = await asyncio.gather(close_attempt(), close_attempt())
    # Idempotent: either one succeeded and the other saw the closed position
    # (returning the same realized_pnl), or both returned the same canonical
    # value — SAME key must never produce two financial mutations.
    oks = [r for r in results if r[0] == "ok"]
    pnls = {r[1] for r in oks}
    assert len(oks) >= 1, f"expected at least one successful close: {results}"
    assert len(pnls) == 1, f"both ok results must produce the SAME pnl: {results}"

    async with factory() as session:
        close_order_count = (await session.execute(
            select(func.count()).select_from(PaperOrder).where(PaperOrder.reduce_only.is_(True))
        )).scalar_one()
        close_ledger_count = (await session.execute(
            select(func.count()).select_from(AccountLedger).where(
                AccountLedger.account_id == account_id,
                AccountLedger.type == LedgerType.TRADE_CLOSE.value,
            )
        )).scalar_one()
        execution_count = (await session.execute(
            select(func.count()).select_from(Execution).where(Execution.position_id == pos_id)
        )).scalar_one()
        assert close_order_count == 1, f"close orders={close_order_count}"
        assert close_ledger_count == 1, f"TRADE_CLOSE ledgers={close_ledger_count}"
        assert execution_count == 1, f"executions={execution_count}"
        pos = await session.get(PaperPosition, pos_id)
        assert pos.status == PositionStatus.CLOSED.value
    await _assert_pg_reconciliation(pg_engine, account_id)


async def test_pg_open_close_ledger_account_atomic(pg_engine):
    """Full atomic cycle on PG: open then close push ledger+account in the same
    savepoint — reconciliation always holds, no partial state."""
    user_id, account_id, comp_id = await _setup(pg_engine, telegram_id=600003)
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with factory() as session:
        account = await session.get(TradingAccount, account_id)
        pos = await open_position(
            session, account, "SOLUSDT", "LONG",
            notional=Decimal("500"), competition_id=comp_id,
            idempotency_key="pg-atomic-1", leverage=10,
        )
        await session.commit()
        await _assert_pg_reconciliation(pg_engine, account_id)
    later = datetime.now(timezone.utc)
    async with factory() as session:
        await persist_snapshot(
            session,
            PriceSnapshot("SOLUSDT", Decimal("100.50"), Decimal("100.60"), Decimal("100.55"), later, later),
        )
        await session.commit()
    async with factory() as session:
        account = await session.get(TradingAccount, account_id)
        position = await session.get(PaperPosition, pos.id)
        await close_position(session, position, account, idempotency_key="pg-atomic-2", reason="manual")
        await session.commit()
        await _assert_pg_reconciliation(pg_engine, account_id)