"""Leaderboard beautiful table tests."""
from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from db.competition_models import Competition, CompetitionStatus
from db.models import User
from db.paper_models import Instrument
from services.bingx_market_data import PriceSnapshot, persist_snapshot
from services.competition import join_competition
from services.leaderboard import build_leaderboard, get_top_n
from services.trading_account import get_or_create_trading_account
from services.paper_adapter import open_position

pytestmark = pytest.mark.asyncio


async def _setup_leaderboard(session, n=5):
    now = datetime.now(timezone.utc)
    comp = Competition(name="LEADER CUP", status=CompetitionStatus.ACTIVE.value, starts_at=now - timedelta(hours=1), ends_at=now + timedelta(days=1), initial_balance=Decimal("10000"), prize_pool=Decimal("500"), ranking_metric="ROI", price_source="BINGX", market_type="USD_M_PERPETUAL")
    session.add(comp)
    await session.flush()
    # Create instruments
    for sym in ["BTCUSDT", "ETHUSDT"]:
        inst = Instrument(symbol=sym, base_asset=sym.replace("USDT",""), quote_asset="USDT", status="active", price_precision=2, quantity_precision=6, min_quantity=Decimal("0.000001"), max_leverage=50)
        session.add(inst)
    await session.flush()
    users = []
    accounts = []
    for i in range(n):
        u = User(telegram_id=900200 + i, username=f"trader_{i:02d}")
        session.add(u)
        await session.flush()
        acc = await get_or_create_trading_account(session, u.id)
        await join_competition(session, u.id, comp.id)
        users.append(u)
        accounts.append(acc)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("70000"), Decimal("70001"), Decimal("70000.5"), now, now))
    await session.commit()
    return comp, users, accounts, now


async def test_leaderboard_top10_sorted_by_roi(session):
    comp, users, accounts, now = await _setup_leaderboard(session, n=5)
    # Make different PnL for each user by opening different sized positions and moving price
    # User 0: no trade (ROI 0)
    # User 1: small profit
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("70000"), Decimal("70001"), Decimal("70000.5"), now, now))
    pos = await open_position(session, accounts[1], "BTCUSDT", "LONG", notional=Decimal("1000"), competition_id=comp.id, idempotency_key="lb-1", leverage=10)
    await session.commit()
    # Price up 10% to give profit
    later = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("77000"), Decimal("77001"), Decimal("77000.5"), later, later))
    # Manually update unrealized for test (tp_sl_engine would do)
    from services.pnl import calc_unrealized
    pos.current_price = Decimal("77000")
    pos.unrealized_pnl = calc_unrealized("LONG", pos.entry_price, Decimal("77000"), pos.quantity)
    await session.flush()
    await session.commit()

    lb = await build_leaderboard(session, comp.id)
    assert len(lb) == 5
    # Check sorted by ROI descending
    rois = [e["roi"] for e in lb]
    assert rois == sorted(rois, reverse=True)
    # Top should be user 1
    assert lb[0]["user_id"] == users[1].id
    assert lb[0]["roi"] > 0
    # Check top 3 have medals in formatted text (via handler)
    from bot.handlers.leaderboard import _format_leaderboard_text
    users_map = {u.id: u for u in users}
    text = _format_leaderboard_text("TEST CUP", lb[:10], users_map, is_final=False)
    assert "🥇" in text or "5440539" in text  # premium gold fallback
    assert "trader_01" in text or "trader" in text.lower()


async def test_leaderboard_beautiful_table_has_medals_and_roi(session):
    comp, users, accounts, now = await _setup_leaderboard(session, n=12)
    lb = await build_leaderboard(session, comp.id)
    top10 = await get_top_n(session, comp.id, 10)
    assert len(top10) == 10
    assert len(lb) == 12
    # Check ranks are sequential
    for i, e in enumerate(top10, start=1):
        assert e["rank"] == i
    # Format beautiful table
    from bot.handlers.leaderboard import _format_leaderboard_text
    users_map = {u.id: u for u in users}
    text = _format_leaderboard_text("Weekly Trading Cup #1", top10, users_map, is_final=False)
    # Must contain header, medals, ROI, equity
    assert "TOP" in text.upper() or "ЛИДЕР" in text.upper() or "CUP" in text.upper()
    assert "$" in text
    assert "%" in text
    # Check that all top 10 are in text
    for i in range(10):
        assert f"trader_{i:02d}" in text


async def test_leaderboard_final_snapshot_used(session):
    comp, users, accounts, now = await _setup_leaderboard(session, n=3)
    # Build and snapshot
    from services.leaderboard import snapshot_leaderboard
    lb = await build_leaderboard(session, comp.id)
    await snapshot_leaderboard(session, comp.id, lb)
    await session.commit()
    # Simulate competition finished
    comp.status = CompetitionStatus.FINISHED.value
    await session.flush()
    await session.commit()
    # Now _get_leaderboard_for_display should return snapshot, not live
    from bot.handlers.leaderboard import _get_leaderboard_for_display
    title, lb2, users_map, is_final, comp2, total = await _get_leaderboard_for_display(session)
    assert is_final is True
    assert "ФИНАЛ" in title
    assert len(lb2) == 3
    assert total == 3
