import asyncio
import logging
from decimal import Decimal
from datetime import datetime, timezone
import ccxt.async_support as ccxt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from config import settings
from db.models import Asset
from services.bingx_market_data import normalize_symbol
from services.metrics import increment

logger = logging.getLogger(__name__)

# Для теста: счётчик последовательных ошибок и флаг алерта.
consecutive_failures: int = 0
last_alert_at: datetime | None = None
ALERT_THRESHOLD = 5  # после N подряд ошибок — алерт админу (в проде — Telegram/лог)

async def sync_assets(engine):
    """Fetch all symbols from BingX via ccxt and upsert into assets + instruments.
    Supports spot (legacy TradeWeek) and swap perpetual (new Trading Game)."""
    exchange = ccxt.bingx({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
    swap_exchange = ccxt.bingx({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
    try:
        await exchange.load_markets()
        for symbol, market in exchange.markets.items():
            if market.get('spot') and market.get('active'):
                bingx_symbol = symbol
                db_symbol = bingx_symbol.replace("/", "-")
                base = market.get('base', '')
                quote = market.get('quote', '')
                async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                    result = await session.execute(select(Asset).where(Asset.symbol == db_symbol))
                    asset = result.scalar_one_or_none()
                    if asset is None:
                        asset = Asset(symbol=db_symbol, base_asset=base, quote_asset=quote, status='active', is_quote_eligible=False, updated_at=datetime.now(timezone.utc))
                        session.add(asset)
                    else:
                        asset.base_asset = base
                        asset.quote_asset = quote
                        asset.status = 'active'
                        asset.updated_at = datetime.now(timezone.utc)
                    await session.commit()
        # swap perpetual for new game -> instruments
        try:
            await swap_exchange.load_markets()
            from db.paper_models import Instrument
            for symbol, market in swap_exchange.markets.items():
                if market.get('swap') and market.get('active'):
                    # e.g. BTC/USDT:USDT -> BTCUSDT for instruments
                    inst_symbol = normalize_symbol(symbol)
                    # keep USDT perpetual only
                    if not inst_symbol.endswith("USDT"):
                        continue
                    base = market.get('base', '')
                    quote = market.get('quote', '')
                    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                        inst = await session.get(Instrument, inst_symbol)
                        if inst is None:
                            inst = Instrument(symbol=inst_symbol, base_asset=base, quote_asset=quote, status='active', price_precision=2, quantity_precision=6, min_quantity=Decimal("0.000001"), created_at=datetime.now(timezone.utc))
                            session.add(inst)
                        else:
                            inst.base_asset = base
                            inst.quote_asset = quote
                            inst.status = 'active'
                        await session.commit()
        except Exception as e:
            logger.warning(f"swap sync failed: {e}")
        logger.info("Assets sync complete")
    finally:
        await exchange.close()
        try:
            await swap_exchange.close()
        except:
            pass

async def fetch_once(exchange, engine, price_cache):
    """
    Один батч fetch_tickers с retry/backoff.
    Обновляет legacy price_cache (last) + новый bingx snapshots (bid/ask) + volume.
    Возвращает True при успехе, False при ошибке.
    """
    global consecutive_failures, last_alert_at
    max_retries = 3
    base_backoff = 0.5
    for attempt in range(max_retries):
        try:
            tickers = await exchange.fetch_tickers()
            now = datetime.now(timezone.utc)
            from services.bingx_market_data import (
                MarketDataInvalid,
                PriceSnapshot,
                normalize_symbol,
                persist_snapshot,
                update_snapshot,
                validate_snapshot,
            )
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            for sym, ticker in tickers.items():
                db_sym = sym.replace("/", "-").split(":", 1)[0]
                price = ticker.get("last") or ticker.get("close")
                bid = ticker.get("bid")
                ask = ticker.get("ask")
                ts = ticker.get("timestamp")
                if price is None or bid is None or ask is None or ts is None:
                    increment("bingx_error")
                    logger.warning("Skipping incomplete BingX ticker %s", sym)
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
                    logger.warning("Skipping invalid BingX ticker %s: %s", sym, exc)
                    continue
                update_snapshot(snap)
                if settings.trading_mode != "paper":
                    # Legacy weekly mode only. Paper execution reads the
                    # shared PostgreSQL snapshot and never this process cache.
                    price_cache.update(db_sym, snap.last, now)
                async with session_factory() as session:
                    await persist_snapshot(session, snap)
                    vol = ticker.get("quoteVolume")
                    if vol is not None:
                        res = await session.execute(select(Asset).where(Asset.symbol == db_sym))
                        asset = res.scalar_one_or_none()
                        if asset:
                            asset.last_24h_quote_volume = Decimal(str(vol))
                            asset.updated_at = now
                            min_vol = Decimal(settings.min_24h_quote_volume_usdt)
                            asset.is_quote_eligible = Decimal(str(vol)) >= min_vol
                    await session.commit()
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
                    logger.error(f"ALERT: BingX unavailable {consecutive_failures} polls in a row — prices stale > {price_cache.max_staleness}s, orders rejected")
                return False
    return False

async def poll_prices(engine, price_cache):
    """Poll BingX perpetual tickers and persist validated shared snapshots."""
    market_type = "swap" if settings.bingx_market_type.lower() in {"perpetual", "swap"} else settings.bingx_market_type
    exchange = ccxt.bingx({
        "enableRateLimit": True,
        "options": {"defaultType": market_type},
    })
    try:
        while True:
            await fetch_once(exchange, engine, price_cache)
            await asyncio.sleep(settings.price_poll_interval_seconds)
    finally:
        await exchange.close()

async def main(engine=None):
    from services.pricing import price_cache
    owns_engine = engine is None
    if engine is None:
        engine = create_async_engine(settings.database_url_async, echo=False)
    try:
        await sync_assets(engine)
        await poll_prices(engine, price_cache)
    finally:
        if owns_engine:
            await engine.dispose()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())

