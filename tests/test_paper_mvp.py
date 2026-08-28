from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from db.competition_models import Competition, CompetitionParticipant
from db.models import User
from db.paper_models import Instrument, PaperOrder, TradingAccount
from services.bingx_market_data import PriceSnapshot, update_snapshot
from services.competition import join_competition
from services.paper_adapter import PaperError, close_position, open_position
from services.trading_account import get_or_create_trading_account

pytestmark = pytest.mark.asyncio


async def _setup(session):
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
        name="TEST CUP",
        status="ACTIVE",
        starts_at=now,
        ends_at=now + timedelta(hours=24),
        initial_balance=Decimal("10000"),
        prize_pool=Decimal("0"),
        ranking_metric="ROI",
        price_source="BINGX",
        market_type="USD_M_PERPETUAL",
    )
    user = User(telegram_id=991001, username="test-paper")
    session.add_all([competition, user])
    await session.flush()
    account = await get_or_create_trading_account(session, user.id)
    await join_competition(session, user.id, competition.id)
    return account, competition, now


async def test_bid_ask_execution_and_close_retry(session):
    account, competition, now = await _setup(session)
    update_snapshot(PriceSnapshot("BTCUSDT", Decimal("50000"), Decimal("50010"), Decimal("50005"), now, now))

    position = await open_position(
        session,
        account,
        "BTCUSDT",
        "LONG",
        notional=Decimal("500"),
        competition_id=competition.id,
        idempotency_key="paper-open-1",
    )
    assert position.entry_price == Decimal("50010.000000000000")
    await session.commit()

    later = datetime.now(timezone.utc)
    update_snapshot(PriceSnapshot("BTCUSDT", Decimal("50100"), Decimal("50110"), Decimal("50105"), later, later))
    closed, pnl = await close_position(session, position, account, idempotency_key="paper-close-1")
    await session.commit()
    assert closed.current_price == Decimal("50100.000000000000")
    assert pnl > 0

    retry, retry_pnl = await close_position(session, position, account, idempotency_key="paper-close-1")
    assert retry.id == position.id
    assert retry_pnl == pnl
    close_orders = await session.execute(
        select(func.count()).select_from(PaperOrder).where(
            PaperOrder.position_id == position.id,
            PaperOrder.reduce_only.is_(True),
        )
    )
    assert close_orders.scalar_one() == 1


async def test_expired_competition_rejects_new_open(session):
    account, competition, now = await _setup(session)
    competition.ends_at = now - timedelta(seconds=1)
    update_snapshot(PriceSnapshot("BTCUSDT", Decimal("50000"), Decimal("50010"), Decimal("50005"), now, now))
    with pytest.raises(PaperError, match="Competition ended"):
        await open_position(
            session,
            account,
            "BTCUSDT",
            "LONG",
            notional=Decimal("500"),
            competition_id=competition.id,
            idempotency_key="expired-open",
        )


async def test_invalid_market_snapshot_rejected():
    from services.bingx_market_data import MarketDataInvalid, validate_snapshot

    now = datetime.now(timezone.utc)
    with pytest.raises(MarketDataInvalid):
        validate_snapshot(PriceSnapshot("BTCUSDT", Decimal("0"), Decimal("1"), Decimal("1"), now, now))
    with pytest.raises(MarketDataInvalid):
        validate_snapshot(PriceSnapshot("BTCUSDT", Decimal("2"), Decimal("1"), Decimal("1"), now, now))
