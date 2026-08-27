from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple
import ccxt.async_support as ccxt

class PriceSnapshot:
    def __init__(self, symbol: str, bid: Decimal | None, ask: Decimal | None, last: Decimal | None, exchange_timestamp: datetime | None, received_at: datetime, source: str = "BINGX"):
        self.symbol = symbol
        self.bid = bid
        self.ask = ask
        self.last = last
        self.exchange_timestamp = exchange_timestamp
        self.received_at = received_at
        self.source = source

# Global cache key: BINGX:PERPETUAL:BTC-USDT
_price_cache: Dict[str, PriceSnapshot] = {}

def _cache_key(symbol: str) -> str:
    # normalize BTCUSDT -> BTC-USDT for key
    s = symbol.replace("/", "-").replace(" ", "")
    if "USDT" in s and "-" not in s:
        s = s.replace("USDT", "-USDT")
    return f"BINGX:PERPETUAL:{s}"

def update_snapshot(snapshot: PriceSnapshot):
    _price_cache[_cache_key(snapshot.symbol)] = snapshot
    # also store normalized variants for lookup
    _price_cache[snapshot.symbol] = snapshot
    _price_cache[snapshot.symbol.replace("-", "")] = snapshot

def get_snapshot(symbol: str) -> Optional[PriceSnapshot]:
    return _price_cache.get(_cache_key(symbol)) or _price_cache.get(symbol) or _price_cache.get(symbol.replace("-", "").replace("/", ""))

def is_stale(snapshot: PriceSnapshot, max_age_ms: int) -> bool:
    if not snapshot.exchange_timestamp:
        # if no exchange ts, use received_at
        age_ms = (datetime.now(timezone.utc) - snapshot.received_at).total_seconds() * 1000
    else:
        age_ms = (datetime.now(timezone.utc) - snapshot.exchange_timestamp).total_seconds() * 1000
    return age_ms > max_age_ms

def get_bid_ask(symbol: str, max_age_ms: int = 2000) -> Tuple[Decimal, Decimal, datetime]:
    snap = get_snapshot(symbol)
    if not snap or snap.bid is None or snap.ask is None:
        raise MarketDataUnavailable(f"Market data unavailable for {symbol} (no bid/ask)")
    if is_stale(snap, max_age_ms):
        raise MarketDataStale(f"Market data stale for {symbol}")
    return snap.bid, snap.ask, snap.exchange_timestamp or snap.received_at

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
        ts = ticker.get('timestamp')
        exchange_ts = datetime.fromtimestamp(ts/1000, tz=timezone.utc) if ts else datetime.now(timezone.utc)
        if bid is None or ask is None:
            raise MarketDataUnavailable(f"No bid/ask for {symbol}")
        return Decimal(str(bid)), Decimal(str(ask)), exchange_ts

    async def fetch_all_tickers(self) -> Dict[str, PriceSnapshot]:
        ex = await self._get_exchange()
        tickers = await ex.fetch_tickers()
        now = datetime.now(timezone.utc)
        result = {}
        for sym, t in tickers.items():
            bid = t.get('bid')
            ask = t.get('ask')
            last = t.get('last') or t.get('close')
            ts = t.get('timestamp')
            exchange_ts = datetime.fromtimestamp(ts/1000, tz=timezone.utc) if ts else now
            snap = PriceSnapshot(
                symbol=sym,
                bid=Decimal(str(bid)) if bid is not None else None,
                ask=Decimal(str(ask)) if ask is not None else None,
                last=Decimal(str(last)) if last is not None else None,
                exchange_timestamp=exchange_ts,
                received_at=now,
                source="BINGX"
            )
            result[sym] = snap
            update_snapshot(snap)
        return result

    async def close(self):
        if self.exchange:
            await self.exchange.close()
            self.exchange = None

# singleton for app
bingx_service = BingXMarketDataService()
