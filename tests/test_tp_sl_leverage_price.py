"""Thorough TP/SL, leverage, and price formatting tests (aiosqlite)."""
from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from db.competition_models import Competition, CompetitionStatus
from db.models import User
from db.paper_models import Instrument, PaperPosition, PositionStatus, TradingAccount
from services.bingx_market_data import PriceSnapshot, persist_snapshot
from services.competition import join_competition
from services.paper_adapter import PaperError, open_position, close_position
from services.trading_account import get_or_create_trading_account
from bot.views import fmt_price, format_side
from workers.tp_sl_engine import check_and_close_positions

pytestmark = pytest.mark.asyncio


async def _setup_competition(session, symbol="BTCUSDT", price_precision=2, max_leverage=50):
    now = datetime.now(timezone.utc)
    # Instrument with specific precision and leverage
    inst = Instrument(
        symbol=symbol,
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        status="active",
        price_precision=price_precision,
        quantity_precision=6,
        min_quantity=Decimal("0.000001"),
        max_leverage=max_leverage,
    )
    session.add(inst)
    comp = Competition(
        name="TP SL CUP",
        status=CompetitionStatus.ACTIVE.value,
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(hours=24),
        initial_balance=Decimal("10000"),
        prize_pool=Decimal("0"),
        ranking_metric="ROI",
        price_source="BINGX",
        market_type="USD_M_PERPETUAL",
    )
    user = User(telegram_id=900001, username="tp_test")
    session.add_all([comp, user])
    await session.flush()
    account = await get_or_create_trading_account(session, user.id)
    await join_competition(session, user.id, comp.id)
    return account, comp, user, now


async def test_format_side_handles_enum_and_string():
    from db.paper_models import PositionSide
    assert format_side(PositionSide.LONG) == "LONG"
    assert format_side(PositionSide.SHORT) == "SHORT"
    assert format_side("LONG") == "LONG"
    assert format_side("SHORT") == "SHORT"
    assert format_side("PositionSide.LONG") == "LONG"
    assert format_side(None) == "—"


async def test_fmt_price_low_price_precision():
    # BTC ~ 70000 should show 2 decimals
    assert fmt_price(Decimal("79256.6")) == "$79,256.60"
    assert fmt_price(Decimal("105.5")) == "$105.5000" or fmt_price(Decimal("105.5")) == "$105.5000"  # 4 decimals for 100-1000
    # UB 0.14 should show 6 decimals to be precise
    low = fmt_price(Decimal("0.14"))
    # Should show 6 decimals: $0.140000
    assert low == "$0.140000"
    # Very low like 0.000008 should show 8
    very_low = fmt_price(Decimal("0.000008"))
    assert very_low == "$0.00000800"
    # With instrument precision
    assert fmt_price(Decimal("0.14"), precision=5) == "$0.14000"
    assert fmt_price(Decimal("105.123456"), precision=2) == "$105.12"
    assert fmt_price(None) == "—"


async def test_fmt_price_with_instrument_precision():
    # Simulate UB with 5 decimals
    assert fmt_price(Decimal("0.14235"), precision=5) == "$0.14235"
    assert fmt_price(Decimal("0.14185"), precision=5) == "$0.14185"
    # Difference should be visible
    assert fmt_price(Decimal("0.14235"), precision=5) != fmt_price(Decimal("0.14185"), precision=5)


async def test_leverage_per_coin_enforced(session):
    # BTC allows 300, UB allows 50 — need different users/competitions
    now = datetime.now(timezone.utc)
    # Manually create two instruments and two users in same competition
    from db.paper_models import Instrument
    # Ensure instruments exist
    for sym, prec, lev in [("BTCUSDT", 2, 300), ("UBUSDT", 5, 50)]:
        inst = await session.get(Instrument, sym)
        if inst is None:
            session.add(Instrument(symbol=sym, base_asset=sym.replace("USDT",""), quote_asset="USDT", status="active", price_precision=prec, quantity_precision=6, min_quantity=Decimal("0.000001"), max_leverage=lev))
        else:
            inst.max_leverage = lev
            inst.price_precision = prec
    comp = Competition(name="LEV TEST", status=CompetitionStatus.ACTIVE.value, starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=24), initial_balance=Decimal("10000"), prize_pool=Decimal("0"), ranking_metric="ROI", price_source="BINGX", market_type="USD_M_PERPETUAL")
    session.add(comp)
    await session.flush()
    # Two users
    user_btc = User(telegram_id=900002, username="lev_btc")
    user_ub = User(telegram_id=900003, username="lev_ub")
    session.add_all([user_btc, user_ub])
    await session.flush()
    btc_acc = await get_or_create_trading_account(session, user_btc.id)
    ub_acc = await get_or_create_trading_account(session, user_ub.id)
    await join_competition(session, user_btc.id, comp.id)
    await join_competition(session, user_ub.id, comp.id)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("70000"), Decimal("70001"), Decimal("70000.5"), now, now))
    await persist_snapshot(session, PriceSnapshot("UBUSDT", Decimal("0.14"), Decimal("0.141"), Decimal("0.1405"), now, now))
    await session.commit()

    # BTC with 300 should succeed
    pos = await open_position(session, btc_acc, "BTCUSDT", "LONG", notional=Decimal("3000"), competition_id=comp.id, idempotency_key="lev-btc-300", leverage=300)
    assert pos.leverage == 300
    await session.commit()

    # UB with 300 should fail (max 50)
    with pytest.raises(PaperError, match="Max leverage"):
        await open_position(session, ub_acc, "UBUSDT", "LONG", notional=Decimal("100"), competition_id=comp.id, idempotency_key="lev-ub-300", leverage=300)
    # UB with 50 should succeed
    pos2 = await open_position(session, ub_acc, "UBUSDT", "LONG", notional=Decimal("100"), competition_id=comp.id, idempotency_key="lev-ub-50", leverage=50)
    assert pos2.leverage == 50


async def test_leverage_global_cap_300(session):
    acc, comp, _, now = await _setup_competition(session, symbol="BTCUSDT", max_leverage=300)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("70000"), Decimal("70001"), Decimal("70000.5"), now, now))
    await session.commit()
    with pytest.raises(PaperError, match="between 1 and 300"):
        await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("100"), competition_id=comp.id, idempotency_key="lev-301", leverage=301)
    with pytest.raises(PaperError):
        await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("100"), competition_id=comp.id, idempotency_key="lev-0", leverage=0)


async def test_tp_sl_long_triggers(session):
    acc, comp, _, now = await _setup_competition(session, symbol="BTCUSDT", max_leverage=50)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("70000"), Decimal("70001"), Decimal("70000.5"), now, now))
    await session.commit()
    # Open LONG at ASK 70001 with TP 71000, SL 69000
    pos = await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("1000"), competition_id=comp.id, idempotency_key="tp-long-open", leverage=10, take_profit=Decimal("71000"), stop_loss=Decimal("69000"))
    assert pos.take_profit == Decimal("71000")
    await session.commit()
    # Price moves up to trigger TP: bid 71000 (LONG closes on bid)
    later = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("71000"), Decimal("71001"), Decimal("71000.5"), later, later))
    await session.commit()
    # Engine should close on TP
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    # Use the same engine via session's bind
    engine = session.bind
    closed = await check_and_close_positions(engine)
    assert closed >= 1
    await session.refresh(pos)
    assert pos.status == PositionStatus.CLOSED.value
    assert pos.realized_pnl > 0


async def test_tp_sl_short_triggers(session):
    acc, comp, _, now = await _setup_competition(session, symbol="BTCUSDT", max_leverage=50)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("70000"), Decimal("70001"), Decimal("70000.5"), now, now))
    await session.commit()
    # SHORT at BID 70000 with TP 69000, SL 71000
    pos = await open_position(session, acc, "BTCUSDT", "SHORT", notional=Decimal("1000"), competition_id=comp.id, idempotency_key="tp-short-open", leverage=10, take_profit=Decimal("69000"), stop_loss=Decimal("71000"))
    await session.commit()
    # Price moves down to trigger TP: ask 69000 (SHORT closes on ask)
    later = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("68999"), Decimal("69000"), Decimal("68999.5"), later, later))
    await session.commit()
    engine = session.bind
    closed = await check_and_close_positions(engine)
    assert closed >= 1
    await session.refresh(pos)
    assert pos.status == PositionStatus.CLOSED.value


async def test_tp_sl_not_triggered_when_not_hit(session):
    acc, comp, _, now = await _setup_competition(session, symbol="BTCUSDT", max_leverage=50)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("70000"), Decimal("70001"), Decimal("70000.5"), now, now))
    await session.commit()
    pos = await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("1000"), competition_id=comp.id, idempotency_key="tp-not-hit", leverage=10, take_profit=Decimal("80000"), stop_loss=Decimal("60000"))
    await session.commit()
    # Price moves a bit but not to TP/SL
    later = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("70500"), Decimal("70501"), Decimal("70500.5"), later, later))
    await session.commit()
    engine = session.bind
    closed = await check_and_close_positions(engine)
    assert closed == 0
    await session.refresh(pos)
    assert pos.status == PositionStatus.OPEN.value


async def test_tp_sl_invalid_rejected(session):
    acc, comp, _, now = await _setup_competition(session, symbol="BTCUSDT", max_leverage=50)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("70000"), Decimal("70001"), Decimal("70000.5"), now, now))
    await session.commit()
    # LONG TP must be > entry (70001), try TP 69000 should fail
    with pytest.raises(PaperError):
        await open_position(session, acc, "BTCUSDT", "LONG", notional=Decimal("100"), competition_id=comp.id, idempotency_key="tp-invalid", leverage=10, take_profit=Decimal("69000"))
    # SHORT TP must be < entry
    with pytest.raises(PaperError):
        await open_position(session, acc, "BTCUSDT", "SHORT", notional=Decimal("100"), competition_id=comp.id, idempotency_key="tp-invalid2", leverage=10, take_profit=Decimal("71000"))


async def test_ub_low_price_pnl_visible(session):
    # Regression for UBUSDT where entry and current both showed $0.14 with 2 decimals, PnL not visible
    acc, comp, _, now = await _setup_competition(session, symbol="UBUSDT", price_precision=5, max_leverage=50)
    await persist_snapshot(session, PriceSnapshot("UBUSDT", Decimal("0.14"), Decimal("0.141"), Decimal("0.1405"), now, now))
    await session.commit()
    pos = await open_position(session, acc, "UBUSDT", "LONG", notional=Decimal("100"), competition_id=comp.id, idempotency_key="ub-low-open", leverage=20)
    # Entry at ASK 0.141
    assert pos.entry_price == Decimal("0.141000000000")
    # Price moves slightly to 0.142
    later = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("UBUSDT", Decimal("0.142"), Decimal("0.1425"), Decimal("0.1422"), later, later))
    await session.commit()
    engine = session.bind
    # Update unrealized via engine
    await check_and_close_positions(engine)
    await session.refresh(pos)
    # With 6 decimals, entry $0.141000 and current $0.142000 should be distinguishable
    assert fmt_price(pos.entry_price, precision=5) != fmt_price(pos.current_price, precision=5)
    assert fmt_price(pos.entry_price, precision=5) == "$0.14100"
    assert fmt_price(pos.current_price, precision=5) == "$0.14200"
    # PnL should be positive and visible
    assert pos.unrealized_pnl > 0
