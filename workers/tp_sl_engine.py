from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

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
from services.notifications import notify_positions_closed
from services.paper_adapter import close_position
from services.pnl import calc_unrealized, is_account_liquidated
from services.trading_account import refresh_account_stats

logger = logging.getLogger(__name__)

PAGE_SIZE = 500


async def check_and_close_positions(
    engine: AsyncEngine, events: list[dict] | None = None
) -> int:
    """Mark open positions and close triggered TP/SL / кросс-ликвидации.

    Кросс-маржа: критерий на уровне аккаунта — суммарный unrealized vs equity.
    Если equity <= 10% депозита (90% съедено) — закрываются ВСЕ открытые
    позиции аккаунта разом (простой и предсказуемый вариант, см. ТЗ).
    Иначе — обычный TP/SL per-position.

    Пагинация по id, каждая страница — отдельная сессия+commit, чтобы не
    голодать позиции при масштабе.
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
            page_events: list[dict] = []
            closed_count += await _process_page(session, positions, page_events)
            await session.commit()
            if events is not None:
                events.extend(page_events)
    return closed_count


async def _process_page(session, positions, events: list[dict] | None = None) -> int:
    closed_count = 0
    # --- 1. Mark all positions в странице текущей ценой (unrealized) ---
    # Группируем по account_id для кросс-проверки
    account_to_positions: dict[int, list[PaperPosition]] = defaultdict(list)
    # close_price кэш per position для TP/SL последующей проверки
    pos_close_price: dict[int, object] = {}
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
        pos_close_price[position.id] = close_price
        # snapshot нужен ещё для idempotency_key timestamp; кэшируем на позиции
        position._snapshot_ts = snapshot.exchange_timestamp  # type: ignore[attr-defined]
        position._snapshot = snapshot  # type: ignore[attr-defined]
        account_to_positions[position.account_id].append(position)

    if not account_to_positions:
        return 0

    # --- 2. Refresh equity per account (сумма unrealized всех OPEN) ---
    accounts: dict[int, TradingAccount] = {}
    for account_id in list(account_to_positions.keys()):
        account = await session.get(TradingAccount, account_id)
        if not account:
            logger.error("Account missing for open position account_id=%s", account_id)
            continue
        await refresh_account_stats(session, account)
        accounts[account_id] = account

    # --- 3. Кросс-ликвидация: equity <= 10% депозита -> закрыть ВСЕ позиции аккаунта ---
    liquidated_account_ids: set[int] = set()
    for account_id, account in accounts.items():
        if is_account_liquidated(account.equity, account.initial_balance):
            liquidated_account_ids.add(account_id)

    # Закрываем все позиции ликвидированных аккаунтов (включая те, что вне текущей страницы)
    for account_id in liquidated_account_ids:
        account = accounts[account_id]
        # Берём все OPEN позиции аккаунта (не только из page), чтобы закрыть разом
        result = await session.execute(
            select(PaperPosition).where(
                PaperPosition.account_id == account_id,
                PaperPosition.status == PositionStatus.OPEN.value,
            )
        )
        all_open = result.scalars().all()
        for pos in all_open:
            try:
                async with session.begin_nested():
                    await close_position(
                        session,
                        pos,
                        account,
                        idempotency_key=f"liq:{pos.id}",
                        reason="LIQUIDATION",
                    )
                closed_count += 1
                if events is not None:
                    events.append(
                        {
                            "position_id": pos.id,
                            "user_id": account.user_id,
                            "symbol": pos.symbol,
                            "side": pos.side,
                            "leverage": pos.leverage,
                            "pnl": pos.realized_pnl,
                            "reason": "LIQUIDATION",
                        }
                    )
                increment("liquidation_triggered")
                logger.info(
                    "Cross liquidation closed position %s %s %s (equity %s <= threshold)",
                    pos.id,
                    pos.symbol,
                    pos.side,
                    account.equity,
                )
            except Exception:
                logger.exception("Cross liquidation close failed for position %s", pos.id)

    # --- 4. Обычный TP/SL для оставшихся (не ликвидированных) позиций из страницы ---
    for position in positions:
        if position.account_id in liquidated_account_ids:
            continue  # уже закрыта в кросс-ликвидации
        if position.id not in pos_close_price:
            continue  # snapshot failed — не трогаем
        close_price = pos_close_price[position.id]
        snapshot = getattr(position, "_snapshot", None)
        # Определяем TP/SL причину
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
        account = accounts.get(position.account_id)
        if not account:
            continue
        # snapshot ts для ключа
        ts = getattr(position, "_snapshot_ts", None)
        import datetime

        ts_str = ts.isoformat() if ts is not None else datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            async with session.begin_nested():
                await close_position(
                    session,
                    position,
                    account,
                    idempotency_key=f"tp_sl:{position.id}:{ts_str}:{reason}",
                    reason=reason,
                )
            closed_count += 1
            if events is not None:
                events.append(
                    {
                        "position_id": position.id,
                        "user_id": account.user_id,
                        "symbol": position.symbol,
                        "side": position.side,
                        "leverage": position.leverage,
                        "pnl": position.realized_pnl,
                        "reason": reason,
                    }
                )
            if reason == "TP":
                increment("tp_triggered")
            else:
                increment("sl_triggered")
            logger.info(
                "TP/SL closed position %s %s %s at %s (%s)",
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
            events: list[dict] = []
            try:
                await check_and_close_positions(engine, events)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("TP/SL loop error")
            if events:
                try:
                    await notify_positions_closed(engine, events)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Close notifications failed")
            await asyncio.sleep(1)
    finally:
        if owns_engine:
            await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())
