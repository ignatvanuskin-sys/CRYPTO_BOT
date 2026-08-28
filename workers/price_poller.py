import asyncio
import logging
from decimal import Decimal
from datetime import datetime, timezone
import ccxt.async_support as ccxt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from config import settings
from services.bingx_market_data import (
    MarketDataInvalid,
    PriceSnapshot,
    normalize_symbol,
    persist_snapshot,
    update_snapshot,
    validate_snapshot,
)
from services.metrics import increment

logger = logging.getLogger(__name__)

# Последовательные ошибки фида и метка последнего алерта.
consecutive_failures: int = 0
last_alert_at: datetime | None = None
ALERT_THRESHOLD = 5  # после N подряд ошибок — критичный алерт в лог

async def sync_instruments(engine: AsyncEngine):
    """Fetch USDT perpetual symbols from BingX via ccxt and upsert into instruments."""
    exchange = ccxt.bingx({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
    try:
        await exchange.load_markets()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        from db.paper_models import Instrument
        for symbol, market in exchange.markets.items():
            if market.get('swap') and market.get('active'):
                # e.g. BTC/USDT:USDT -> BTCUSDT for instruments
                inst_symbol = normalize_symbol(symbol)
                # keep USDT perpetual only
                if not inst_symbol.endswith("USDT"):
                    continue
                base = market.get('base', '')
                quote = market.get('quote', '')
                try:
                    async with factory() as session:
                        inst = await session.get(Instrument, inst_symbol)
                        if inst is None:
                            inst = Instrument(
                                symbol=inst_symbol,
                                base_asset=base,
                                quote_asset=quote,
                                status='active',
                                price_precision=2,
                                quantity_precision=6,
                                min_quantity=Decimal("0.000001"),
                                created_at=datetime.now(timezone.utc),
                            )
                            session.add(inst)
                        else:
                            inst.base_asset = base
                            inst.quote_asset = quote
                            inst.status = 'active'
                        await session.commit()
                except Exception as exc:
                    logger.warning("Skipping instrument %s: %s", inst_symbol, exc)
        logger.info("Instruments sync complete")
    finally:
        await exchange.close()

async def fetch_once(exchange, engine: AsyncEngine) -> bool:
    """
    Один батч fetch_tickers с retry/backoff.
    Обновляет shared PostgreSQL snapshot (bid/ask/last) — единственный
    источник цен для исполнения и UI. Возвращает True при успехе.
    """
    global consecutive_failures, last_alert_at
    max_retries = 3
    base_backoff = 0.5
    for attempt in range(max_retries):
        try:
            tickers = await exchange.fetch_tickers()
            now = datetime.now(timezone.utc)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            for sym, ticker in tickers.items():
                price = ticker.get("last") or ticker.get("close")
                bid = ticker.get("bid")
                ask = ticker.get("ask")
                ts = ticker.get("timestamp")
                if price is None or bid is None or ask is None or ts is None:
                    increment("bingx_ticker_incomplete")
                    continue
                exchange_ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                snap = PriceSnapshot(
                    symbol=normalize_symbol(sym),
                    bid=Decimal(str(bid)),
                    ask=Decimal(str(ask)),
                    last=Decimal(str(price)),
                    exchange_timestamp=exchange_ts,
                    received_at=now,
                )
                try:
                    validate_snapshot(snap)
                except MarketDataInvalid as exc:
                    increment("bingx_ticker_invalid")
                    logger.warning("Skipping invalid BingX ticker %s: %s", sym, exc)
                    continue
                # process-local cache only for local sqlite tests;
                # production execution reads the PostgreSQL snapshot
                update_snapshot(snap)
                try:
                    async with factory() as session:
                        await persist_snapshot(session, snap)
                        await session.commit()
                except Exception as exc:
                    increment("bingx_snapshot_persist_failed")
                    logger.warning("Snapshot persist failed for %s: %s", snap.symbol, exc)
            consecutive_failures = 0
            return True
        except Exception as e:
            # rate limit / timeout / network
            is_last = attempt == max_retries - 1
            wait = base_backoff * (2 ** attempt)
            increment("bingx_error")
            logger.warning(f"price poll attempt {attempt+1}/{max_retries} failed: {e}; retry in {wait:.1f}s")
            if not is_last:
                await asyncio.sleep(wait)
            else:
                consecutive_failures += 1
                if consecutive_failures >= ALERT_THRESHOLD:
                    last_alert_at = datetime.now(timezone.utc)
                    logger.error(
                        f"ALERT: BingX unavailable {consecutive_failures} polls in a row — "
                        f"orders on stale/unavailable market data are rejected"
                    )
                return False
    return False

async def poll_prices(engine: AsyncEngine) -> None:
    """Poll BingX perpetual tickers and persist validated shared snapshots."""
    market_type = "swap" if settings.bingx_market_type.lower() in {"perpetual", "swap"} else settings.bingx_market_type
    exchange = ccxt.bingx({
        "enableRateLimit": True,
        "options": {"defaultType": market_type},
    })
    try:
        while True:
            await fetch_once(exchange, engine)
            await asyncio.sleep(settings.price_poll_interval_seconds)
    finally:
        await exchange.close()

async def run_forever(engine: AsyncEngine) -> None:
    """Background task entrypoint for the single bot process."""
    await sync_instruments(engine)
    await poll_prices(engine)
