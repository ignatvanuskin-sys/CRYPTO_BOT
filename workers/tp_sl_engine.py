from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from config import settings
from db.paper_models import PaperPosition, PositionStatus, TradingAccount
from services.bingx_market_data import (
    MarketDataInvalid,
    MarketDataStale,
    MarketDataUnavailable,
    get_execution_snapshot,
)
from services.metrics import increment
from services.paper_adapter import close_position
from services.pnl import calc_unrealized
from services.trading_account import refresh_account_stats

logger = logging.getLogger(__name__)

PAGE_SIZE = 500


async def check_and_close_positions(engine: AsyncEngine) -> int:
    """Mark open positions and close triggered TP/SL positions once.

    Paginates through ALL open positions (PAGE_SIZE per page, keyed by id)
    so no position is starved from price-marking / liquidation / TP-SL
    checks at scale. Each page is its own session + commit.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    closed_count = 0
    last_id = 0
    while True:
        async with factory() as session:
            result = await session.execute(
                select(PaperPosition)
                .where(
                    PaperPosition.status == PositionStatus.OPEN.value,
                    PaperPosition.id > last_id,
                )
                .order_by(PaperPosition.id)
                .limit(PAGE_SIZE)
            )
            positions = result.scalars().all()
            if not positions:
                break
            last_id = positions[-1].id
            closed_count += await _process_page(session, positions)
            await session.commit()
    return closed_count


async def _process_page(session, positions) -> int:
    closed_count = 0
    for position in positions:
        try:
            snapshot = await get_execution_snapshot(
                session, position.symbol, settings.market_data_max_age_ms
            )
        except MarketDataStale:
            increment("stale_price_rejected")
            continue
        except (MarketDataUnavailable, MarketDataInvalid):
            increment("bingx_error")
            continue

        close_price = snapshot.bid if position.side == "LONG" else snapshot.ask
        position.current_price = close_price
        position.unrealized_pnl = calc_unrealized(
            position.side,
            position.entry_price,
            close_price,
            position.quantity,
        )
        account = await session.get(TradingAccount, position.account_id)
        if not account:
            logger.error("Account missing for open position %s", position.id)
            continue
        await refresh_account_stats(session, account)

        # Liquidation check — 90% of margin lost (protects from negative return_amount)
        margin = (position.notional / Decimal(str(position.leverage or 1))).quantize(Decimal("0.01"))
        if position.unrealized_pnl <= -margin * Decimal("0.9"):
            reason = "LIQUIDATION"
        else:
            reason = None
            if position.side == "LONG":
                if position.take_profit is not None and close_price >= position.take_profit:
                    reason = "TP"
                elif position.stop_loss is not None and close_price <= position.stop_loss:
                    reason = "SL"
            else:
                if position.take_profit is not None and close_price <= position.take_profit:
                    reason = "TP"
                elif position.stop_loss is not None and close_price >= position.stop_loss:
                    reason = "SL"

            if reason is None:
                continue
        try:
            async with session.begin_nested():
                await close_position(
                    session,
                    position,
                    account,
                    idempotency_key=f"tp_sl:{position.id}:{snapshot.exchange_timestamp.isoformat()}:{reason}",
                    reason=reason,
                )
            closed_count += 1
            if reason == "LIQUIDATION":
                increment("liquidation_triggered")
            elif reason == "TP":
                increment("tp_triggered")
            else:
                increment("sl_triggered")
            logger.info(
                "TP/SL/Liquidation closed position %s %s %s at %s (%s)",
                position.id,
                position.symbol,
                position.side,
                close_price,
                reason,
            )
        except Exception:
            logger.exception("TP/SL close failed for position %s", position.id)
    return closed_count


async def run_forever(engine: AsyncEngine | None = None) -> None:
    owns_engine = engine is None
    if engine is None:
        engine = create_async_engine(settings.database_url_async, echo=False)
    try:
        while True:
            try:
                await check_and_close_positions(engine)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("TP/SL loop error")
            await asyncio.sleep(1)
    finally:
        if owns_engine:
            await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())
