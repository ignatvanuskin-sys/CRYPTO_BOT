"""Liquidation tests — high leverage without TP/SL must not crash on close."""
from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from db.competition_models import Competition, CompetitionStatus, Execution, ExecutionReason
from db.models import User
from db.paper_models import Instrument, PaperPosition, PositionStatus
from services.bingx_market_data import PriceSnapshot, persist_snapshot
from services.competition import join_competition
from services.paper_adapter import PaperError, open_position, close_position
from services.trading_account import get_or_create_trading_account
from workers.tp_sl_engine import check_and_close_positions

pytestmark = pytest.mark.asyncio


async def _setup(session, leverage=300, symbol="BTCUSDT", price="100.00"):
    now = datetime.now(timezone.utc)
    inst = Instrument(symbol=symbol, base_asset="BTC", quote_asset="USDT", status="active", price_precision=2, quantity_precision=6, min_quantity=Decimal("0.000001"), max_leverage=300)
    # Upsert
    existing = await session.get(Instrument, symbol)
    if existing is None:
        session.add(inst)
    else:
        existing.max_leverage = 300
        existing.price_precision = 2
    comp = Competition(name="LIQ CUP", status=CompetitionStatus.ACTIVE.value, starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=24), initial_balance=Decimal("10000"), prize_pool=Decimal("0"), ranking_metric="ROI", price_source="BINGX", market_type="USD_M_PERPETUAL")
    user = User(telegram_id=910000, username="liq_test")
    session.add_all([comp, user])
    await session.flush()
    acc = await get_or_create_trading_account(session, user.id)
    await join_competition(session, user.id, comp.id)
    await persist_snapshot(session, PriceSnapshot(symbol, Decimal(price), Decimal(price), Decimal(price), now, now))
    # Actually need bid/ask: for LONG open uses ASK, so set bid/ask around price
    # Use price as both bid/ask for simplicity, but need realistic spread
    # We'll set bid = price, ask = price * 1.001
    bid = (Decimal(price) * Decimal("0.999")).quantize(Decimal("0.0001"))
    ask = (Decimal(price) * Decimal("1.001")).quantize(Decimal("0.0001"))
    # Overwrite snapshot with proper bid/ask
    await persist_snapshot(session, PriceSnapshot(symbol, bid, ask, Decimal(price), now, now))
    await session.commit()
    return acc, comp, user, now


async def test_high_leverage_no_crash_on_adverse_close(session):
    """P0.1: 300x LONG, price crashes to 0 — close must cap loss at margin, not crash on CHECK."""
    acc, comp, _, now = await _setup(session, leverage=300, symbol="BTCUSDT", price="100")
    # Open LONG 300x with budget 100 => notional 30000, qty 300, margin 100
    # Use ASK for open
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("99"), Decimal("100"), Decimal("99.5"), now, now))
    pos = await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("3000"), competition_id=comp.id, idempotency_key="liq-open-1", leverage=300)
    await session.commit()
    # Simulate crash: price goes to 1 (bid 1, ask 1.01) — would cause huge loss
    crash = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("1"), Decimal("1.01"), Decimal("1.005"), crash, crash))
    await session.commit()
    # Close should not crash, should cap return at 0
    closed, net = await close_position(session, pos, acc, idempotency_key="liq-close-1", reason="manual")
    await session.commit()
    # Net should be capped at -margin (100) not -2990
    # margin = 10 for this notional? Actually notional 3000, leverage 300 => margin 10
    # Wait notional 3000, leverage 300 => margin 10, not 100. Let's compute: 3000/300=10
    # Gross = (1 - 100)*30 = -2970, net = -2970, but capped to -10, return 0
    assert closed.realized_pnl == -Decimal("10.00") or closed.realized_pnl == Decimal("-10.00")
    assert acc.cash_balance >= Decimal("0")
    # Check ledger balance_after still >=0
    assert acc.cash_balance >= 0


async def test_liquidation_engine_triggers(session):
    """P0.1: tp_sl_engine should liquidate when unrealized <= -90% margin."""
    acc, comp, _, now = await _setup(session, leverage=300, symbol="BTCUSDT", price="100")
    # Use more realistic: open with budget 100, leverage 300 => margin 100, notional 30000? Actually budget is margin, not notional
    # Our open uses notional, so we need to compute: budget 100, leverage 300 => notional 30000, qty 300
    # But our setup uses notional 3000 with leverage 300 => margin 10, easier to test
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("99"), Decimal("100"), Decimal("99.5"), now, now))
    pos = await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("3000"), competition_id=comp.id, idempotency_key="liq-engine-open", leverage=300)
    await session.commit()
    # Price moves against: entry 100, current bid 99.7 => unrealized = (99.7-100)*30 = -9, margin 10, 90% threshold -9, should trigger
    trigger_time = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("99.6"), Decimal("99.7"), Decimal("99.65"), trigger_time, trigger_time))
    await session.commit()
    # Engine should close
    engine = session.bind
    closed_count = await check_and_close_positions(engine)
    assert closed_count >= 1
    await session.refresh(pos)
    assert pos.status == PositionStatus.CLOSED.value
    # Check execution reason is LIQUIDATION
    result = await session.execute(select(Execution).where(Execution.position_id == pos.id, Execution.execution_reason == ExecutionReason.LIQUIDATION.value))
    exec_row = result.scalar_one_or_none()
    assert exec_row is not None, "Liquidation execution not found"


async def test_liquidation_does_not_trigger_prematurely(session):
    acc, comp, _, now = await _setup(session, leverage=10, symbol="BTCUSDT", price="100")
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("99"), Decimal("100"), Decimal("99.5"), now, now))
    pos = await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("1000"), competition_id=comp.id, idempotency_key="liq-notrigger", leverage=10)
    await session.commit()
    # Small move: entry 100, current 99.5 => unrealized -5, margin 100, 90% threshold -90, should NOT trigger
    later = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("99.4"), Decimal("99.5"), Decimal("99.45"), later, later))
    await session.commit()
    engine = session.bind
    closed = await check_and_close_positions(engine)
    assert closed == 0
    await session.refresh(pos)
    assert pos.status == PositionStatus.OPEN.value
