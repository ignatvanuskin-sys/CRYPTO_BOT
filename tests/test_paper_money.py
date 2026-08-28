"""Paper money tests for the single-demo flow (aiosqlite, non-concurrent).

Covers: leverage margin reservation/return, insufficient margin rejection,
idempotent demo balance grant (one account + one INITIAL_BALANCE ledger row).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from db.competition_models import Competition
from db.models import User
from db.paper_models import AccountLedger, Instrument, LedgerType, TradingAccount
from services.bingx_market_data import PriceSnapshot, persist_snapshot
from services.competition import join_competition
from services.paper_adapter import InsufficientMargin, close_position, open_position
from services.trading_account import get_or_create_trading_account

pytestmark = pytest.mark.asyncio


async def _setup(session, user_id=555001):
    now = datetime.now(timezone.utc)
    session.add(
        Instrument(
            symbol="SOLUSDT",
            base_asset="SOL",
            quote_asset="USDT",
            status="active",
            min_quantity=Decimal("0.000001"),
        )
    )
    competition = Competition(
        name="LEV CUP",
        status="ACTIVE",
        starts_at=now,
        ends_at=now + timedelta(hours=24),
        initial_balance=Decimal("10000"),
        prize_pool=Decimal("0"),
        ranking_metric="ROI",
        price_source="BINGX",
        market_type="USD_M_PERPETUAL",
    )
    user = User(telegram_id=user_id, username="lev-user")
    session.add_all([competition, user])
    await session.flush()
    # shared snapshot: bid 100, ask 100.10
    await persist_snapshot(
        session,
        PriceSnapshot("SOLUSDT", Decimal("100"), Decimal("100.10"), Decimal("100.05"), now, now),
    )
    account = await get_or_create_trading_account(session, user.id)
    await join_competition(session, user.id, competition.id)
    return account, competition


async def test_leverage_reserves_budget_as_margin(session):
    account, competition = await _setup(session)
    # budget 500 with 5x -> notional 2500, margin reserved 500
    position = await open_position(
        session,
        account,
        "SOLUSDT",
        "LONG",
        notional=Decimal("2500"),
        competition_id=competition.id,
        idempotency_key="lev-open-1",
        leverage=5,
    )
    await session.commit()
    await session.refresh(account)
    assert position.leverage == Decimal("5")
    assert position.notional == Decimal("2500.00")
    # entry at ASK 100.10 -> qty = 2500 / 100.10
    assert position.entry_price == Decimal("100.100000000000")
    assert account.margin_used == Decimal("500.00")
    assert account.cash_balance == Decimal("9500.00")
    assert account.available_margin == Decimal("9500.00")

    # close: returns margin + pnl
    closed, pnl = await close_position(
        session, position, account, idempotency_key="lev-close-1"
    )
    await session.commit()
    await session.refresh(account)
    assert account.margin_used == Decimal("0")
    # pnl = (100.00 - 100.10) * qty
    assert pnl < 0
    assert account.cash_balance == (Decimal("10000.00") + pnl).quantize(Decimal("0.01"))


async def test_insufficient_margin_rejected(session):
    account, competition = await _setup(session)
    with pytest.raises(InsufficientMargin):
        await open_position(
            session,
            account,
            "SOLUSDT",
            "LONG",
            notional=Decimal("20000"),  # margin 20000 > 10000
            competition_id=competition.id,
            idempotency_key="lev-open-2",
            leverage=1,
        )


async def test_invalid_leverage_rejected(session):
    account, competition = await _setup(session)
    with pytest.raises(Exception, match="[Ll]everage"):
        await open_position(
            session,
            account,
            "SOLUSDT",
            "LONG",
            notional=Decimal("500"),
            competition_id=competition.id,
            idempotency_key="lev-open-3",
            leverage=0,
        )


async def test_demo_grant_is_idempotent(session):
    account, _ = await _setup(session, user_id=555002)
    again = await get_or_create_trading_account(session, account.user_id)
    await session.commit()
    assert again.id == account.id
    ledger_count = (
        await session.execute(
            select(func.count()).select_from(AccountLedger).where(
                AccountLedger.account_id == account.id,
                AccountLedger.type == LedgerType.INITIAL_BALANCE.value,
            )
        )
    ).scalar_one()
    assert ledger_count == 1
    assert account.cash_balance == Decimal("10000")
