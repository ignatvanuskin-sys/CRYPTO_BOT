import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from config import settings
from db.paper_models import PaperPosition, PositionStatus
from services.pricing import price_cache

logger = logging.getLogger(__name__)

async def check_and_close_positions(engine):
    from services.paper_adapter import close_position
    from db.paper_models import TradingAccount
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        result = await session.execute(select(PaperPosition).where(PaperPosition.status == PositionStatus.OPEN.value))
        positions = result.scalars().all()
        for pos in positions:
            entry = price_cache.get(pos.symbol)
            if not entry:
                # try dash variant
                for cand in [pos.symbol.replace("USDT", "-USDT"), pos.symbol]:
                    entry = price_cache.get(cand)
                    if entry:
                        break
            if not entry:
                continue
            price, _ = entry
            should_close = False
            reason = ""
            if pos.side == "LONG":
                if pos.take_profit is not None and price >= pos.take_profit:
                    should_close = True
                    reason = "TP"
                elif pos.stop_loss is not None and price <= pos.stop_loss:
                    should_close = True
                    reason = "SL"
            else:  # SHORT
                if pos.take_profit is not None and price <= pos.take_profit:
                    should_close = True
                    reason = "TP"
                elif pos.stop_loss is not None and price >= pos.stop_loss:
                    should_close = True
                    reason = "SL"
            if should_close:
                try:
                    # lock and close
                    acc = await session.get(TradingAccount, pos.account_id)
                    if not acc:
                        continue
                    # update current_price / unrealized before close
                    from services.pnl import calc_unrealized
                    pos.current_price = price
                    pos.unrealized_pnl = calc_unrealized(pos.side, pos.entry_price, price, pos.quantity)
                    await session.flush()
                    await close_position(session, pos, acc, idempotency_key=f"tp_sl:{pos.id}:{price}", reason=reason)
                    await session.commit()
                    logger.info(f"TP/SL closed position {pos.id} {pos.symbol} {pos.side} at {price} reason {reason}")
                    # TODO: send Telegram notification via NotificationService
                except Exception as e:
                    await session.rollback()
                    logger.warning(f"TP/SL close failed for {pos.id}: {e}")

async def run_forever():
    engine = create_async_engine(settings.database_url_async, echo=False)
    while True:
        try:
            await check_and_close_positions(engine)
        except Exception as e:
            logger.error(f"tp_sl loop error: {e}")
        await asyncio.sleep(1)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())
