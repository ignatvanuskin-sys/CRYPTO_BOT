"""Liquidation tests — кросс-маржа: ликвидация для всего бюджета (90% депозита)."""
from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from db.competition_models import Competition, CompetitionStatus, Execution, ExecutionReason
from db.models import User
from db.paper_models import Instrument, PaperPosition, PositionStatus, AccountLedger, LedgerType
from services.bingx_market_data import PriceSnapshot, persist_snapshot
from services.competition import join_competition
from services.paper_adapter import open_position, close_position
from services.pnl import cross_liquidation_threshold
from services.trading_account import get_or_create_trading_account, refresh_account_stats
from workers.tp_sl_engine import check_and_close_positions

pytestmark = pytest.mark.asyncio


async def _setup(session, leverage=300, symbol="BTCUSDT", price="100.00", telegram_id=910000):
    now = datetime.now(timezone.utc)
    inst = Instrument(symbol=symbol, base_asset="BTC", quote_asset="USDT", status="active", price_precision=2, quantity_precision=6, min_quantity=Decimal("0.000001"), max_leverage=300)
    existing = await session.get(Instrument, symbol)
    if existing is None:
        session.add(inst)
    else:
        existing.max_leverage = 300
        existing.price_precision = 2
    comp = Competition(name="LIQ CUP", status=CompetitionStatus.ACTIVE.value, starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=24), initial_balance=Decimal("10000"), prize_pool=Decimal("0"), ranking_metric="ROI", price_source="BINGX", market_type="USD_M_PERPETUAL")
    user = User(telegram_id=telegram_id, username="liq_test")
    session.add_all([comp, user])
    await session.flush()
    acc = await get_or_create_trading_account(session, user.id)
    await join_competition(session, user.id, comp.id)
    bid = (Decimal(price) * Decimal("0.999")).quantize(Decimal("0.0001"))
    ask = (Decimal(price) * Decimal("1.001")).quantize(Decimal("0.0001"))
    await persist_snapshot(session, PriceSnapshot(symbol, bid, ask, Decimal(price), now, now))
    await session.commit()
    return acc, comp, user, now


async def test_high_leverage_no_crash_on_adverse_close(session):
    """Гэп: 300x LONG краш до 1 — close капает убыток, cash не в минус."""
    acc, comp, _, now = await _setup(session, leverage=300, symbol="BTCUSDT", price="100", telegram_id=910001)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("99"), Decimal("100"), Decimal("99.5"), now, now))
    pos = await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("3000"), competition_id=comp.id, idempotency_key="liq-open-1", leverage=300)
    await session.commit()
    crash = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("1"), Decimal("1.01"), Decimal("1.005"), crash, crash))
    await session.commit()
    closed, net = await close_position(session, pos, acc, idempotency_key="liq-close-1", reason="manual")
    await session.commit()
    # margin 10, gross -2970 -> capped -10
    assert closed.realized_pnl == Decimal("-10.00")
    assert acc.cash_balance >= Decimal("0")


async def test_cross_single_position_triggers_at_90pct_deposit(session):
    """Одна позиция, кросс — ликвидация когда equity <= 10% депозита (~90% съедено).
    Открываем маржу 9000 из 10000, затем движение -9000 -> equity 1000 -> ликвидация."""
    acc, comp, _, now = await _setup(session, leverage=300, symbol="BTCUSDT", price="100", telegram_id=910002)
    # Open with notional 9000 lev1 => margin 9000 (почти all-in, но остаётся 1000 cash)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("99"), Decimal("100"), Decimal("99.5"), now, now))
    pos = await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("9000"), competition_id=comp.id, idempotency_key="liq-single-open", leverage=1)
    await session.commit()
    await session.refresh(acc)
    threshold = cross_liquidation_threshold(acc.initial_balance)
    assert threshold == Decimal("1000.00")
    # unrealized -9000 => equity 1000 -> должен триггерить
    # entry 100, qty 90 (9000/100), price 0? need -9000 => price = entry + unrealized/qty = 100 -100 =0 impossible
    # Use 27000 notional lev3 margin 9000 qty 270 entry 100 price 66.66 => -9000
    # We opened 9000 lev1 qty 90, need -9000 => price 0 -> cap; better use lev3 case
    # Recreate with lev3: close and reopen as lev3 (easier: open another test with correct notional)
    # For simplicity, crash to 1 -> unrealized -8910 -> equity 1090 still above 1000 not trigger, need lower
    # Let's crash to 0.1 -> unrealized -8991 -> equity 1009 close to threshold
    # We'll just move to 1 and then second step to trigger: we force account equity below threshold by second position? Instead we directly test via engine with price low enough
    crash = datetime.now(timezone.utc)
    # Use notional 27000 lev3 scenario instead: reopen
    # Cleanup current pos by closing it manually then open correct
    await close_position(session, pos, acc, idempotency_key="liq-single-close-temp", reason="manual")
    await session.commit()
    # Replenish account for all-in test: reset cash to 10000 via ledger? Simpler: create new user for this scenario
    # Create fresh account with large margin
    acc2, comp2, user2, _ = await _setup(session, leverage=3, symbol="ETHUSDT", price="100", telegram_id=910003)
    await persist_snapshot(session, PriceSnapshot("ETHUSDT", Decimal("99"), Decimal("100"), Decimal("99.5"), datetime.now(timezone.utc), datetime.now(timezone.utc)))
    pos2 = await open_position(session, acc2, "ETHUSDT", "LONG", notional=Decimal("27000"), competition_id=comp2.id, idempotency_key="liq-single-27000", leverage=3)
    await session.commit()
    await session.refresh(acc2)
    # qty = 270, entry ~100, price 66.66 => unrealized -9000
    trigger = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("ETHUSDT", Decimal("66.6"), Decimal("66.66"), Decimal("66.63"), trigger, trigger))
    await session.commit()
    engine = session.bind
    closed_count = await check_and_close_positions(engine)
    assert closed_count >= 1
    await session.refresh(pos2)
    assert pos2.status == PositionStatus.CLOSED.value
    result = await session.execute(select(Execution).where(Execution.position_id == pos2.id, Execution.execution_reason == ExecutionReason.LIQUIDATION.value))
    assert result.scalar_one_or_none() is not None


async def test_cross_does_not_trigger_prematurely(session):
    """Малое движение -5 при депозите 10000 не должно ликвидировать (запас 9000)."""
    acc, comp, _, now = await _setup(session, leverage=10, symbol="BTCUSDT", price="100", telegram_id=910004)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("99"), Decimal("100"), Decimal("99.5"), now, now))
    pos = await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("1000"), competition_id=comp.id, idempotency_key="liq-notrigger2", leverage=10)
    await session.commit()
    later = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("99.4"), Decimal("99.5"), Decimal("99.45"), later, later))
    await session.commit()
    engine = session.bind
    closed = await check_and_close_positions(engine)
    assert closed == 0
    await session.refresh(pos)
    assert pos.status == PositionStatus.OPEN.value


async def test_cross_multiple_positions_account_level(session):
    """Несколько позиций: суммарный убыток съедает депозит -> закрываются ВСЕ разом."""
    acc, comp, _, now = await _setup(session, leverage=10, symbol="BTCUSDT", price="100", telegram_id=910005)
    inst2 = Instrument(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT", status="active", price_precision=2, quantity_precision=6, min_quantity=Decimal("0.000001"), max_leverage=300)
    existing2 = await session.get(Instrument, "ETHUSDT")
    if existing2 is None:
        session.add(inst2)
    await persist_snapshot(session, PriceSnapshot("ETHUSDT", Decimal("99"), Decimal("100"), Decimal("99.5"), now, now))
    await session.commit()
    # Две позиции lev3: notional 9000 => margin 3000 каждая, всего 6000, cash 4000, порог 1000
    # Чтобы опустить equity до 1000 нужно sum_unrealized <= -9000 => -4500 каждая
    # entry 100 qty 90 => price 50 => -4500
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("99"), Decimal("100"), Decimal("99.5"), now, now))
    pos1 = await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("9000"), competition_id=comp.id, idempotency_key="liq-multi-1", leverage=3)
    await session.commit()
    pos2 = await open_position(session, acc, "ETHUSDT", "LONG", notional=Decimal("9000"), competition_id=comp.id, idempotency_key="liq-multi-2", leverage=3)
    await session.commit()
    await session.refresh(acc)
    # Move both to 50 (bid 50 ask 50.05)
    trigger = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("50"), Decimal("50.05"), Decimal("50.02"), trigger, trigger))
    await persist_snapshot(session, PriceSnapshot("ETHUSDT", Decimal("50"), Decimal("50.05"), Decimal("50.02"), trigger, trigger))
    await session.commit()
    engine = session.bind
    closed = await check_and_close_positions(engine)
    assert closed >= 2
    await session.refresh(pos1)
    await session.refresh(pos2)
    assert pos1.status == PositionStatus.CLOSED.value
    assert pos2.status == PositionStatus.CLOSED.value
    # Both have LIQUIDATION executions
    r1 = await session.execute(select(Execution).where(Execution.position_id == pos1.id, Execution.execution_reason == ExecutionReason.LIQUIDATION.value))
    r2 = await session.execute(select(Execution).where(Execution.position_id == pos2.id, Execution.execution_reason == ExecutionReason.LIQUIDATION.value))
    assert r1.scalar_one_or_none() is not None
    assert r2.scalar_one_or_none() is not None


async def test_cross_gap_account_level_cash_never_negative(session):
    """Гэп на уровне аккаунта: суммарный убыток > депозита капается, cash >=0, есть ADJUSTMENT."""
    # Депозит 20, две позиции margin 10 каждая => порог 2, гэп -5940 => ликвидация + кап
    acc, comp, _, now = await _setup(session, leverage=300, symbol="BTCUSDT", price="100", telegram_id=910006)
    # Уменьшаем депозит до 20 до открытия позиций (чтобы всё влезло и порог был 2)
    acc.initial_balance = Decimal("20.00")
    acc.cash_balance = Decimal("20.00")
    acc.equity = Decimal("20.00")
    acc.available_margin = Decimal("20.00")
    await session.flush()
    await session.commit()
    inst2 = Instrument(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT", status="active", price_precision=2, quantity_precision=6, min_quantity=Decimal("0.000001"), max_leverage=300)
    if await session.get(Instrument, "ETHUSDT") is None:
        session.add(inst2)
    await persist_snapshot(session, PriceSnapshot("ETHUSDT", Decimal("99"), Decimal("100"), Decimal("99.5"), now, now))
    await session.commit()
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("99"), Decimal("100"), Decimal("99.5"), now, now))
    pos1 = await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("3000"), competition_id=comp.id, idempotency_key="liq-gap-1", leverage=300)
    pos2 = await open_position(session, acc, "ETHUSDT", "LONG", notional=Decimal("3000"), competition_id=comp.id, idempotency_key="liq-gap-2", leverage=300)
    await session.commit()
    crash = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("1"), Decimal("1.01"), Decimal("1.005"), crash, crash))
    await persist_snapshot(session, PriceSnapshot("ETHUSDT", Decimal("1"), Decimal("1.01"), Decimal("1.005"), crash, crash))
    await session.commit()
    await refresh_account_stats(session, acc)
    # equity ~ 20 -5940 = -5920 <= threshold 2 -> должна сработать кросс-ликвидация
    engine = session.bind
    closed = await check_and_close_positions(engine)
    assert closed >= 2
    await session.refresh(acc)
    assert acc.cash_balance >= Decimal("0")
    ledgers = (await session.execute(select(AccountLedger).where(AccountLedger.account_id == acc.id, AccountLedger.type == LedgerType.ADJUSTMENT.value))).scalars().all()
    assert len(ledgers) >= 1
