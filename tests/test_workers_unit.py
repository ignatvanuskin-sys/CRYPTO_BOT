"""workers/price_poller.py — unit tests для helper-функций."""
import pytest

from workers.price_poller import _max_leverage_for_symbol, DEMO_WATCHLIST


class TestMaxLeverage:
    def test_btc(self): assert _max_leverage_for_symbol("BTCUSDT") == 300
    def test_eth(self): assert _max_leverage_for_symbol("ETHUSDT") == 300
    def test_sol(self): assert _max_leverage_for_symbol("SOLUSDT") == 100
    def test_alt(self): assert _max_leverage_for_symbol("UBUSDT") == 50
    def test_unknown(self): assert _max_leverage_for_symbol("XYZUSDT") == 50


class TestWatchlist:
    def test_has_btc(self): assert "BTCUSDT" in DEMO_WATCHLIST
    def test_has_sol(self): assert "SOLUSDT" in DEMO_WATCHLIST
    def test_size(self): assert 20 <= len(DEMO_WATCHLIST) <= 30
    def test_all_usdt(self): assert all(s.endswith("USDT") for s in DEMO_WATCHLIST)


@pytest.mark.asyncio
async def test_get_relevant_symbols_includes_open_positions(sqlite_engine):
    """Позиции на символах вне watchlist добавляются к списку опроса."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from datetime import datetime, timezone, timedelta
    from decimal import Decimal
    from db.paper_models import PaperPosition, PositionStatus, TradingAccount
    from db.models import User
    from db.competition_models import Competition, CompetitionStatus

    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as session:
        now = datetime.now(timezone.utc)
        comp = Competition(name="WL", status=CompetitionStatus.ACTIVE.value,
                           starts_at=now, ends_at=now + timedelta(days=1),
                           initial_balance=Decimal("10000"), prize_pool=Decimal("0"),
                           ranking_metric="ROI", price_source="BINGX",
                           market_type="USD_M_PERPETUAL")
        user = User(telegram_id=8001, username="wl_test")
        session.add_all([comp, user])
        await session.flush()
        acc = TradingAccount(user_id=user.id, cash_balance=Decimal("10000"),
                             equity=Decimal("10000"), available_margin=Decimal("10000"),
                             initial_balance=Decimal("10000"))
        session.add(acc)
        await session.flush()
        session.add(PaperPosition(account_id=acc.id, symbol="RAREUSDT", side="LONG",
                                  status=PositionStatus.OPEN.value, quantity=Decimal("1"),
                                  entry_price=Decimal("100"), current_price=Decimal("100"),
                                  notional=Decimal("100"), leverage=Decimal("1")))
        await session.commit()

    from workers.price_poller import _get_relevant_symbols
    result = await _get_relevant_symbols(sqlite_engine)
    assert "RAREUSDT" in result
    assert "BTCUSDT" in result