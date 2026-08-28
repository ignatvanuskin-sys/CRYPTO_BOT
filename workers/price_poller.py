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

def _max_leverage_for_symbol(symbol: str) -> int:
    if symbol in ("BTCUSDT", "ETHUSDT"):
        return 300
    if symbol == "SOLUSDT":
        return 100
    # Default for most alts
    return 50


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
                # Derive precisions from market info
                price_prec = 2
                qty_prec = 6
                min_qty = Decimal("0.000001")
                try:
                    # Use market precision if available
                    p = market.get('precision', {})
                    if p.get('price') is not None:
                        # price precision is like 0.1, 0.001, 1e-05
                        price_step = Decimal(str(p['price']))
                        # Convert step to decimals: 0.1 -> 1, 0.001 -> 3, 1e-05 -> 5
                        price_prec = max(0, -price_step.as_tuple().exponent)
                    if p.get('amount') is not None:
                        amt_step = Decimal(str(p['amount']))
                        qty_prec = max(0, -amt_step.as_tuple().exponent)
                    # min quantity from limits or info
                    limits = market.get('limits', {})
                    amt_limits = limits.get('amount', {})
                    if amt_limits and amt_limits.get('min') is not None:
                        min_qty = Decimal(str(amt_limits['min']))
                    else:
                        info = market.get('info', {})
                        if info.get('tradeMinQuantity'):
                            min_qty = Decimal(str(info['tradeMinQuantity']))
                except Exception:
                    pass
                max_lev = _max_leverage_for_symbol(inst_symbol)
                if 'batch_instruments' not in locals():
                    batch_instruments = []
                batch_instruments.append((inst_symbol, base, quote, price_prec, qty_prec, min_qty, max_lev))
        # Batch upsert all instruments in one transaction
        if 'batch_instruments' in locals() and batch_instruments:
            try:
                async with factory() as session:
                    for inst_symbol, base, quote, price_prec, qty_prec, min_qty, max_lev in batch_instruments:
                        inst = await session.get(Instrument, inst_symbol)
                        if inst is None:
                            inst = Instrument(
                                symbol=inst_symbol,
                                base_asset=base,
                                quote_asset=quote,
                                status='active',
                                price_precision=price_prec,
                                quantity_precision=qty_prec,
                                min_quantity=min_qty,
                                max_leverage=max_lev,
                                created_at=datetime.now(timezone.utc),
                            )
                            session.add(inst)
                        else:
                            inst.base_asset = base
                            inst.quote_asset = quote
                            inst.status = 'active'
                            inst.price_precision = price_prec
                            inst.quantity_precision = qty_prec
                            inst.min_quantity = min_qty
                            if inst.max_leverage < max_lev:
                                inst.max_leverage = max_lev
                    await session.commit()
            except Exception as exc:
                logger.warning("Batch instruments sync failed: %s", exc)
        logger.info("Instruments sync complete")
    finally:
        await exchange.close()

DEMO_WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
    "LTCUSDT", "BCHUSDT", "XLMUSDT", "ETCUSDT", "FILUSDT",
    "TRXUSDT", "XMRUSDT", "ATOMUSDT", "VETUSDT", "ICPUSDT",
    "UBUSDT", "1000PEPEUSDT", "SHIBUSDT", "MATICUSDT", "OPUSDT",
]


async def _get_relevant_symbols(engine: AsyncEngine) -> list[str]:
    """Symbols to poll: demo watchlist + any symbol with open position."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            # Open positions
            from db.paper_models import PaperPosition, PositionStatus

            result = await session.execute(
                select(PaperPosition.symbol).where(PaperPosition.status == PositionStatus.OPEN.value).distinct()
            )
            open_syms = {row[0] for row in result.all()}
            # Combine with watchlist, dedupe
            all_syms = set(DEMO_WATCHLIST) | open_syms
            # Filter to only those that exist as active instruments (avoid polling delisted)
            if all_syms:
                from db.paper_models import Instrument

                res = await session.execute(
                    select(Instrument.symbol).where(Instrument.symbol.in_(list(all_syms)), Instrument.status == "active")
                )
                active = {r[0] for r in res.all()}
                # If none of the watchlist is yet in instruments (first run), fallback to watchlist
                return list(active) if active else list(all_syms)
            return []
    except Exception:
        return DEMO_WATCHLIST


async def fetch_once(exchange, engine: AsyncEngine) -> bool:
    """
    Один батч fetch_tickers с retry/backoff.
    Обновляет shared PostgreSQL snapshot (bid/ask/last) — единственный
    источник цен для исполнения и UI. Возвращает True при успехе.
    Для демо опрашиваем только DEMO_WATCHLIST + символы с открытыми позициями (~20-30 вместо 950).
    """
    global consecutive_failures, last_alert_at
    max_retries = 3
    base_backoff = 0.5
    for attempt in range(max_retries):
        try:
            # Определяем релевантные символы для этого цикла
            relevant = await _get_relevant_symbols(engine)
            # Map DB symbols (BTCUSDT) → CCXT symbols (BTC/USDT:USDT) for fetch
            ccxt_symbols = None
            if relevant:
                # Build CCXT symbol list via markets (if loaded)
                try:
                    if exchange.markets:
                        ccxt_symbols = []
                        for db_sym in relevant:
                            # Find market with normalized symbol == db_sym
                            for ccxt_sym, m in exchange.markets.items():
                                if normalize_symbol(ccxt_sym) == db_sym and m.get("swap"):
                                    ccxt_symbols.append(ccxt_sym)
                                    break
                        if not ccxt_symbols:
                            ccxt_symbols = None
                except Exception:
                    ccxt_symbols = None
            if ccxt_symbols:
                tickers = await exchange.fetch_tickers(ccxt_symbols)
            else:
                tickers = await exchange.fetch_tickers()
            now = datetime.now(timezone.utc)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
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
                        await persist_snapshot(session, snap)
                    except Exception as exc:
                        increment("bingx_snapshot_persist_failed")
                        logger.warning("Snapshot persist failed for %s: %s", snap.symbol, exc)
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
