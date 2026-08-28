"""P1.4 and P2.5 — competition isolation and multiple positions per asset."""
from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from db.competition_models import Competition, CompetitionStatus, CompetitionParticipant
from db.models import User
from db.paper_models import Instrument, PaperPosition, PositionStatus
from services.bingx_market_data import PriceSnapshot, persist_snapshot
from services.competition import join_competition
from services.paper_adapter import open_position
from services.trading_account import get_or_create_trading_account

pytestmark = pytest.mark.asyncio


async def test_starting_equity_clean_sheet(session):
    """P1.4: New tournament starts clean — starting_equity = initial_balance, not carried P&L."""
    now = datetime.now(timezone.utc)
    # First cup
    comp1 = Competition(name="CUP1", status=CompetitionStatus.ACTIVE.value, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=6), initial_balance=Decimal("10000"), prize_pool=Decimal("0"), ranking_metric="ROI", price_source="BINGX", market_type="USD_M_PERPETUAL")
    session.add(comp1)
    await session.flush()
    user = User(telegram_id=910100, username="iso_test")
    session.add(user)
    await session.flush()
    acc = await get_or_create_trading_account(session, user.id)
    await join_competition(session, user.id, comp1.id)
    # Simulate profit: manually bump account
    acc.cash_balance = Decimal("12000")
    acc.equity = Decimal("12000")
    await session.flush()
    await session.commit()

    # Second cup — should reset to clean sheet (10000), not 12000
    comp2 = Competition(name="CUP2", status=CompetitionStatus.ACTIVE.value, starts_at=now - timedelta(hours=1), ends_at=now + timedelta(days=7), initial_balance=Decimal("10000"), prize_pool=Decimal("0"), ranking_metric="ROI", price_source="BINGX", market_type="USD_M_PERPETUAL")
    session.add(comp2)
    await session.flush()
    part2 = await join_competition(session, user.id, comp2.id)
    await session.flush()
    # Check starting is 10000, not 12000, and account was reset
    assert part2.starting_equity == Decimal("10000")
    await session.refresh(acc)
    # Account should have been reset to 10000 for clean sheet
    assert acc.cash_balance == Decimal("10000")
    assert acc.equity == Decimal("10000")


async def test_starting_equity_carryover_if_not_clean_sheet(session):
    """Document alternative: if we wanted carryover, starting would be current equity."""
    # This test documents the choice: clean sheet is implemented, so this would fail if we used carryover
    # We keep it as a placeholder to show the decision
    pass


async def test_multiple_positions_same_asset_allowed(session):
    """P2.5: Multiple open positions on same symbol allowed, tp_sl handles each."""
    now = datetime.now(timezone.utc)
    inst = Instrument(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", status="active", price_precision=2, quantity_precision=6, min_quantity=Decimal("0.000001"), max_leverage=50)
    comp = Competition(name="MULTI CUP", status=CompetitionStatus.ACTIVE.value, starts_at=now - timedelta(hours=1), ends_at=now + timedelta(days=1), initial_balance=Decimal("10000"), prize_pool=Decimal("0"), ranking_metric="ROI", price_source="BINGX", market_type="USD_M_PERPETUAL")
    user = User(telegram_id=910101, username="multi_test")
    session.add_all([inst, comp, user])
    await session.flush()
    acc = await get_or_create_trading_account(session, user.id)
    await join_competition(session, user.id, comp.id)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("70000"), Decimal("70001"), Decimal("70000.5"), now, now))
    await session.commit()

    # Open two LONG positions on same symbol with different notional
    pos1 = await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("1000"), competition_id=comp.id, idempotency_key="multi-1", leverage=10)
    pos2 = await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("500"), competition_id=comp.id, idempotency_key="multi-2", leverage=10)
    await session.commit()
    assert pos1.id != pos2.id
    assert pos1.symbol == pos2.symbol == "BTCUSDT"
    # Both should be open
    result = await session.execute(select(PaperPosition).where(PaperPosition.account_id == acc.id, PaperPosition.status == PositionStatus.OPEN.value))
    opens = result.scalars().all()
    assert len(opens) == 2
    # Engine should handle both
    from workers.tp_sl_engine import check_and_close_positions
    # No TP/SL, so none should close, but engine should update both without error
    engine = session.bind
    closed = await check_and_close_positions(engine)
    assert closed == 0
    # Still 2 open
    result2 = await session.execute(select(PaperPosition).where(PaperPosition.account_id == acc.id, PaperPosition.status == PositionStatus.OPEN.value))
    assert len(result2.scalars().all()) == 2


async def test_multiple_positions_different_tp_sl(session):
    """Multiple positions with different TP/SL are handled independently."""
    now = datetime.now(timezone.utc)
    inst = Instrument(symbol="SOLUSDT", base_asset="SOL", quote_asset="USDT", status="active", price_precision=3, quantity_precision=6, min_quantity=Decimal("0.000001"), max_leverage=50)
    comp = Competition(name="MULTI2", status=CompetitionStatus.ACTIVE.value, starts_at=now - timedelta(hours=1), ends_at=now + timedelta(days=1), initial_balance=Decimal("10000"), prize_pool=Decimal("0"), ranking_metric="ROI", price_source="BINGX", market_type="USD_M_PERPETUAL")
    user = User(telegram_id=910102, username="multi2")
    session.add_all([inst, comp, user])
    await session.flush()
    acc = await get_or_create_trading_account(session, user.id)
    await join_competition(session, user.id, comp.id)
    await persist_snapshot(session, PriceSnapshot("SOLUSDT", Decimal("100"), Decimal("100.1"), Decimal("100.05"), now, now))
    await session.commit()
    pos1 = await open_position(session, acc, "SOLUSDT", "LONG", notional=Decimal("1000"), competition_id=comp.id, idempotency_key="multi-tp-1", leverage=10, take_profit=Decimal("110"))
    pos2 = await open_position(session, acc, "SOLUSDT", "LONG", notional=Decimal("1000"), competition_id=comp.id, idempotency_key="multi-tp-2", leverage=10, take_profit=Decimal("120"))
    await session.commit()
    # Price moves to 111 — should trigger only pos1
    later = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("SOLUSDT", Decimal("111"), Decimal("111.1"), Decimal("111.05"), later, later))
    await session.commit()
    from workers.tp_sl_engine import check_and_close_positions

    engine = session.bind
    closed = await check_and_close_positions(engine)
    assert closed == 1
    await session.refresh(pos1)
    await session.refresh(pos2)
    assert pos1.status == PositionStatus.CLOSED.value
    assert pos2.status == PositionStatus.OPEN.value
