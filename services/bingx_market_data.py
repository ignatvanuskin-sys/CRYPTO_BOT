from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession
import ccxt.async_support as ccxt

from db.market_data import MarketSnapshot

class PriceSnapshot:
    def __init__(self, symbol: str, bid: Decimal | None, ask: Decimal | None, last: Decimal | None, exchange_timestamp: datetime | None, received_at: datetime, source: str = "BINGX"):
        self.symbol = symbol
        self.bid = bid
        self.ask = ask
        self.last = last
        self.exchange_timestamp = exchange_timestamp
        self.received_at = received_at
        self.source = source

# Global cache key: BINGX:PERPETUAL:BTC-USDT. The cache is only a
# presentation/local-test cache; production execution reads PostgreSQL.
_price_cache: Dict[str, PriceSnapshot] = {}


def normalize_symbol(symbol: str) -> str:
    """Normalize CCXT symbols such as BTC/USDT:USDT to BTCUSDT."""
    value = symbol.upper().replace(" ", "")
    value = value.split(":", 1)[0].replace("/", "").replace("-", "")
    return value


def _cache_key(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    if normalized.endswith("USDT"):
        normalized = normalized[:-4] + "-USDT"
    return f"BINGX:PERPETUAL:{normalized}"

def update_snapshot(snapshot: PriceSnapshot):
    _price_cache[_cache_key(snapshot.symbol)] = snapshot
    # Also store normalized variants for local UI/test lookups.
    _price_cache[snapshot.symbol] = snapshot
    _price_cache[normalize_symbol(snapshot.symbol)] = snapshot

def get_snapshot(symbol: str) -> Optional[PriceSnapshot]:
    return (
        _price_cache.get(_cache_key(symbol))
        or _price_cache.get(symbol)
        or _price_cache.get(normalize_symbol(symbol))
    )


def _age_ms(ts: Optional[datetime]) -> float:
    """Age of a timestamp in milliseconds, tz-safe.

    Database backends return naive datetimes (SQLite stores strings; PostgreSQL
    TIMESTAMP WITHOUT TIME ZONE is naive). Exchange timestamps are UTC, so a
    naive value is treated as UTC. Comparing a tz-aware 'now' against a naive
    timestamp would raise TypeError, so normalize first.
    """
    if ts is None:
        return float("inf")
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() * 1000


def validate_snapshot(snapshot: PriceSnapshot, max_future_ms: int = 5000) -> None:
    """Reject malformed, inverted, missing, stale/future market snapshots."""
    if snapshot.bid is None or snapshot.ask is None or snapshot.last is None:
        raise MarketDataInvalid(f"Incomplete market snapshot for {snapshot.symbol}")
    if not snapshot.bid.is_finite() or not snapshot.ask.is_finite() or not snapshot.last.is_finite():
        raise MarketDataInvalid(f"Non-finite market snapshot for {snapshot.symbol}")
    if snapshot.bid <= 0 or snapshot.ask <= 0 or snapshot.last <= 0:
        raise MarketDataInvalid(f"Non-positive market snapshot for {snapshot.symbol}")
    if snapshot.ask < snapshot.bid:
        raise MarketDataInvalid(f"Inverted bid/ask for {snapshot.symbol}")
    if snapshot.exchange_timestamp is None:
        raise MarketDataInvalid(f"Missing exchange timestamp for {snapshot.symbol}")
    age_ms = _age_ms(snapshot.exchange_timestamp)
    if age_ms < -max_future_ms:
        raise MarketDataInvalid(f"Future market timestamp for {snapshot.symbol}")


def is_stale(snapshot: PriceSnapshot, max_age_ms: int) -> bool:
    ts = snapshot.exchange_timestamp or snapshot.received_at
    age_ms = _age_ms(ts)
    return age_ms > max_age_ms or age_ms < -5000


def get_bid_ask(symbol: str, max_age_ms: int = 2000) -> Tuple[Decimal, Decimal, datetime]:
    snap = get_snapshot(symbol)
    if snap is None:
        raise MarketDataUnavailable(f"Market data unavailable for {symbol} (no snapshot)")
    validate_snapshot(snap)
    if is_stale(snap, max_age_ms):
        raise MarketDataStale(f"Market data stale for {symbol}")
    return snap.bid, snap.ask, snap.exchange_timestamp

def get_price_for_side(symbol: str, side: str, max_age_ms: int = 2000) -> Tuple[Decimal, datetime]:
    bid, ask, ts = get_bid_ask(symbol, max_age_ms)
    if side == "LONG":
        # LONG OPEN = ASK, CLOSE = BID — caller decides, but for open we return ask
        return ask, ts
    else:
        return bid, ts

class MarketDataUnavailable(Exception):
    pass

class MarketDataStale(Exception):
    pass


class MarketDataInvalid(Exception):
    pass


async def persist_snapshot(session: AsyncSession, snapshot: PriceSnapshot) -> None:
    """Upsert one validated snapshot into the shared PostgreSQL table."""
    validate_snapshot(snapshot)
    symbol = normalize_symbol(snapshot.symbol)
    row = await session.get(MarketSnapshot, symbol)
    values = {
        "source": snapshot.source,
        "market_type": "PERPETUAL",
        "bid": snapshot.bid,
        "ask": snapshot.ask,
        "last": snapshot.last,
        "exchange_timestamp": snapshot.exchange_timestamp,
        "received_at": snapshot.received_at,
        "updated_at": snapshot.received_at,
    }
    if row is None:
        session.add(MarketSnapshot(symbol=symbol, **values))
    else:
        for key, value in values.items():
            setattr(row, key, value)


async def get_shared_snapshot(
    session: AsyncSession,
    symbol: str,
    max_age_ms: int,
) -> PriceSnapshot:
    """Read the canonical snapshot shared across bot/API/worker processes."""
    row = await session.get(MarketSnapshot, normalize_symbol(symbol))
    if row is None:
        raise MarketDataUnavailable(f"Market data unavailable for {symbol}")
    if row.source != "BINGX" or row.market_type != "PERPETUAL":
        raise MarketDataInvalid(f"Unsupported market data source for {symbol}")
    snapshot = PriceSnapshot(
        symbol=row.symbol,
        bid=Decimal(str(row.bid)),
        ask=Decimal(str(row.ask)),
        last=Decimal(str(row.last)),
        exchange_timestamp=row.exchange_timestamp,
        received_at=row.received_at,
        source=row.source,
    )
    validate_snapshot(snapshot)
    if is_stale(snapshot, max_age_ms):
        raise MarketDataStale(f"Market data stale for {symbol}")
    return snapshot


async def get_execution_snapshot(
    session: AsyncSession,
    symbol: str,
    max_age_ms: int,
) -> PriceSnapshot:
    """Use shared DB data in production; local cache is test-only fallback."""
    try:
        return await get_shared_snapshot(session, symbol, max_age_ms)
    except (MarketDataUnavailable, MarketDataStale, MarketDataInvalid):
        dialect = session.bind.dialect.name if session.bind else ""
        if dialect == "sqlite":
            snapshot = get_snapshot(symbol)
            if snapshot is None:
                # Test-only compatibility for legacy unit tests. Production
                # PostgreSQL never synthesizes a snapshot from local memory.
                from services.pricing import price_cache

                try:
                    price, timestamp = price_cache.get_price_or_raise(symbol)
                    snapshot = PriceSnapshot(
                        symbol=symbol,
                        bid=price,
                        ask=price,
                        last=price,
                        exchange_timestamp=timestamp,
                        received_at=datetime.now(timezone.utc),
                    )
                except Exception:
                    snapshot = None
            if snapshot is not None:
                validate_snapshot(snapshot)
                if not is_stale(snapshot, max_age_ms):
                    return snapshot
        raise


class BingXMarketDataService:
    def __init__(self, market_type: str = "perpetual"):
        self.market_type = market_type
        self.exchange = None

    async def _get_exchange(self):
        if self.exchange is None:
            # Use swap type for perpetual
            self.exchange = ccxt.bingx({
                'enableRateLimit': True,
                'options': {'defaultType': 'swap'},
            })
            await self.exchange.load_markets()
        return self.exchange

    async def get_symbols(self) -> list[str]:
        ex = await self._get_exchange()
        # filter swap active
        syms = []
        for m in ex.markets.values():
            if m.get('swap') and m.get('active'):
                syms.append(m['symbol'])
        return syms

    async def get_ticker(self, symbol: str) -> dict:
        ex = await self._get_exchange()
        return await ex.fetch_ticker(symbol)

    async def get_bid_ask(self, symbol: str) -> Tuple[Decimal, Decimal, datetime]:
        ticker = await self.get_ticker(symbol)
        bid = ticker.get('bid')
        ask = ticker.get('ask')
        ts = ticker.get("timestamp")
        if not ts:
            raise MarketDataUnavailable(f"No exchange timestamp for {symbol}")
        exchange_ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        if bid is None or ask is None:
            raise MarketDataUnavailable(f"No bid/ask for {symbol}")
        snapshot = PriceSnapshot(
            symbol=symbol,
            bid=Decimal(str(bid)),
            ask=Decimal(str(ask)),
            last=Decimal(str(ticker.get("last") or ticker.get("close") or bid)),
            exchange_timestamp=exchange_ts,
            received_at=datetime.now(timezone.utc),
        )
        validate_snapshot(snapshot)
        return snapshot.bid, snapshot.ask, snapshot.exchange_timestamp

    async def fetch_all_tickers(self) -> Dict[str, PriceSnapshot]:
        ex = await self._get_exchange()
        tickers = await ex.fetch_tickers()
        now = datetime.now(timezone.utc)
        result = {}
        for sym, t in tickers.items():
            bid = t.get('bid')
            ask = t.get('ask')
            last = t.get('last') or t.get('close')
            ts = t.get("timestamp")
            if not ts:
                continue
            exchange_ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            snap = PriceSnapshot(
                symbol=sym,
                bid=Decimal(str(bid)) if bid is not None else None,
                ask=Decimal(str(ask)) if ask is not None else None,
                last=Decimal(str(last)) if last is not None else None,
                exchange_timestamp=exchange_ts,
                received_at=now,
                source="BINGX"
            )
            try:
                validate_snapshot(snap)
            except MarketDataInvalid:
                continue
            result[sym] = snap
            update_snapshot(snap)
        return result

    async def close(self):
        if self.exchange:
            await self.exchange.close()
            self.exchange = None

# singleton for app
bingx_service = BingXMarketDataService()
