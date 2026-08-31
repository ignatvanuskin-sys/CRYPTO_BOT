"""services/bingx_market_data.py — normalize, validate, snapshot lifecycle."""
from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pytest

from services.bingx_market_data import (
    normalize_symbol, validate_snapshot, is_stale, update_snapshot,
    get_snapshot, PriceSnapshot, persist_snapshot, get_shared_snapshot,
    get_execution_snapshot, MarketDataStale, MarketDataUnavailable, MarketDataInvalid,
)


class TestNormalizeSymbol:
    @pytest.mark.parametrize("raw,expected", [
        ("BTC/USDT:USDT", "BTCUSDT"),
        ("SOL/USDT:SOL", "SOLUSDT"),
        ("ETH/USDT", "ETHUSDT"),
        ("BTCUSDT", "BTCUSDT"),
        ("1000PEPE/USDT:USDT", "1000PEPEUSDT"),
        ("btc/usdt:usdt", "BTCUSDT"),
    ])
    def test_normalization(self, raw, expected):
        assert normalize_symbol(raw) == expected


class TestValidateSnapshot:
    def _snap(self, bid, ask, last=None, ts="auto"):
        if ts == "auto":
            ts = datetime.now(timezone.utc)
        return PriceSnapshot("BTCUSDT", Decimal(bid), Decimal(ask),
                             Decimal(last or bid), ts,
                             datetime.now(timezone.utc))
    def test_valid(self): validate_snapshot(self._snap("100", "101"))  # no raise
    def test_inverted(self):
        with pytest.raises(MarketDataInvalid): validate_snapshot(self._snap("101", "100"))
    def test_zero_bid(self):
        with pytest.raises(MarketDataInvalid): validate_snapshot(self._snap("0", "1"))
    def test_negative(self):
        with pytest.raises(MarketDataInvalid): validate_snapshot(self._snap("-1", "1"))
    def test_missing_ts(self):
        with pytest.raises(MarketDataInvalid): validate_snapshot(self._snap("100", "101", ts=None))
    def test_future_ts(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=10)
        with pytest.raises(MarketDataInvalid): validate_snapshot(self._snap("100", "101", ts=future))


class TestIsStale:
    def _snap(self, ts_offset_s):
        return PriceSnapshot("X", Decimal("1"), Decimal("2"), Decimal("1.5"),
                             datetime.now(timezone.utc) + timedelta(seconds=ts_offset_s),
                             datetime.now(timezone.utc))

    def test_fresh(self): assert not is_stale(self._snap(0), 2000)
    def test_stale(self): assert is_stale(self._snap(-5), 3000)
    def test_boundary(self): assert not is_stale(self._snap(-1), 2000)


class TestSnapshotStore:
    def test_update_and_get(self):
        now = datetime.now(timezone.utc)
        snap = PriceSnapshot("SOLUSDT", Decimal("1"), Decimal("2"), Decimal("1.5"), now, now)
        update_snapshot(snap)
        got = get_snapshot("SOLUSDT")
        assert got is not None
        assert got.bid == Decimal("1")

    def test_get_missing(self):
        assert get_snapshot("NOSUCH") is None


class TestSharedSnapshot:
    @pytest.mark.asyncio
    async def test_roundtrip(self, session):
        now = datetime.now(timezone.utc)
        snap = PriceSnapshot("SOLUSDT", Decimal("1"), Decimal("2"), Decimal("1.5"), now, now)
        await persist_snapshot(session, snap)
        await session.commit()
        got = await get_shared_snapshot(session, "SOLUSDT", 6000)
        assert got.bid == Decimal("1")
        assert got.ask == Decimal("2")

    @pytest.mark.asyncio
    async def test_stale_rejected(self, session):
        old = datetime.now(timezone.utc) - timedelta(seconds=30)
        snap = PriceSnapshot("SOLUSDT", Decimal("1"), Decimal("2"), Decimal("1.5"), old, old)
        await persist_snapshot(session, snap)
        await session.commit()
        with pytest.raises(MarketDataStale):
            await get_shared_snapshot(session, "SOLUSDT", 1000)

    @pytest.mark.asyncio
    async def test_missing_rejected(self, session):
        with pytest.raises(MarketDataUnavailable):
            await get_shared_snapshot(session, "NOSUCHUSDT", 6000)