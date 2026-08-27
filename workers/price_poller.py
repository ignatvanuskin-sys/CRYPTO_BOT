import asyncio
import logging
from decimal import Decimal
from datetime import datetime, timezone
import ccxt.async_support as ccxt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from config import settings
from db.models import Asset

logger = logging.getLogger(__name__)

# Для теста: счётчик последовательных ошибок и флаг алерта.
consecutive_failures: int = 0
last_alert_at: datetime | None = None
ALERT_THRESHOLD = 5  # после N подряд ошибок — алерт админу (в проде — Telegram/лог)

async def sync_assets(engine):
    """Fetch all spot symbols from BingX via ccxt and upsert into assets."""
    exchange = ccxt.bingx({'enableRateLimit': True})
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
        logger.info("Assets sync complete")
    finally:
        await exchange.close()

async def fetch_once(exchange, engine, price_cache):
    """
    Один батч fetch_tickers с retry/backoff.
    Возвращает True при успехе, False при ошибке.
    При ошибке — не падает, инкрементит consecutive_failures, логирует.
    Цены не обновляются, поэтому быстро протухают и ордера отклоняются (MAX_PRICE_STALENESS_SECONDS).
    """
    global consecutive_failures, last_alert_at
    max_retries = 3
    base_backoff = 0.5
    for attempt in range(max_retries):
        try:
            tickers = await exchange.fetch_tickers()
            now = datetime.now(timezone.utc)
            for sym, ticker in tickers.items():
                db_sym = sym.replace("/", "-")
                price = ticker.get('last') or ticker.get('close')
                if price is not None:
                    price_cache.update(db_sym, Decimal(str(price)), now)
                vol = ticker.get('quoteVolume')
                if vol is not None:
                    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
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
    """Poll tickers batch and update cache + eligibility daily. Не падает целиком при ошибках ccxt."""
    exchange = ccxt.bingx({'enableRateLimit': True})
    try:
        while True:
            await fetch_once(exchange, engine, price_cache)
            await asyncio.sleep(settings.price_poll_interval_seconds)
    finally:
        await exchange.close()

async def main():
    from services.pricing import price_cache
    engine = create_async_engine(settings.database_url_async, echo=False)
    await sync_assets(engine)
    await poll_prices(engine, price_cache)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())

