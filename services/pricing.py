from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

class PriceCache:
    def __init__(self, max_staleness_seconds: int = 3):
        self.max_staleness = max_staleness_seconds
        self._prices: Dict[str, tuple[Decimal, datetime]] = {}

    def update(self, symbol: str, price: Decimal, ts: datetime | None = None):
        if ts is None:
            ts = datetime.now(timezone.utc)
        self._prices[symbol] = (Decimal(str(price)), ts)

    def update_batch(self, tickers: Dict[str, Decimal], ts: datetime | None = None):
        if ts is None:
            ts = datetime.now(timezone.utc)
        for sym, price in tickers.items():
            self._prices[sym] = (Decimal(str(price)), ts)

    def get(self, symbol: str) -> Optional[tuple[Decimal, datetime]]:
        return self._prices.get(symbol)

    def get_price_or_raise(self, symbol: str) -> tuple[Decimal, datetime]:
        entry = self._prices.get(symbol)
        if entry is None:
            raise PriceNotAvailable(f"Price not available for {symbol}")
        price, ts = entry
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > self.max_staleness:
            raise PriceStale(f"Price for {symbol} is stale: {age:.1f}s > {self.max_staleness}s")
        return price, ts

    def is_stale(self, symbol: str) -> bool:
        entry = self._prices.get(symbol)
        if entry is None:
            return True
        _, ts = entry
        return (datetime.now(timezone.utc) - ts).total_seconds() > self.max_staleness

    def clear(self):
        self._prices.clear()

class PriceNotAvailable(Exception):
    pass

class PriceStale(Exception):
    pass

# global cache instance
price_cache = PriceCache()

def set_max_staleness(seconds: int):
    price_cache.max_staleness = seconds
