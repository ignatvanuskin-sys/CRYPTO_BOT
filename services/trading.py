from __future__ import annotations
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from db.models import User, Week, Asset, Transaction, Order, Position, TransactionType, OrderStatus, OrderSide
from db.repo import get_cash_balance, get_position, lock_user_week
from services.pricing import price_cache, PriceStale, PriceNotAvailable
from services.accounts import ensure_can_trade

# quantizers
QTY_QUANT = Decimal("0.0000000001")
PRICE_QUANT = Decimal("0.0000000001")
USD_QUANT = Decimal("0.01")

class TradingError(Exception):
    pass

class InsufficientFunds(TradingError):
    pass

class InsufficientPosition(TradingError):
    pass

class StalePrice(TradingError):
    pass

async def _get_active_week(session: AsyncSession) -> Week:
    result = await session.execute(select(Week).where(Week.status == "active").order_by(Week.id.desc()).limit(1))
    week = result.scalar_one_or_none()
    if not week:
        # auto-create first week if not exists (for tests)
        week = Week(week_number=1, starts_at=datetime.now(timezone.utc), ends_at=datetime.now(timezone.utc), status="active")
        session.add(week)
        await session.flush()
    return week

async def execute_buy(
    session: AsyncSession,
    user: User,
    symbol: str,
    notional_usd: Decimal,
    idempotency_key: str,
) -> Order:
    ensure_can_trade(user)
    notional_usd = Decimal(str(notional_usd)).quantize(USD_QUANT)
    if notional_usd <= 0:
        raise TradingError("Notional must be positive")

    week = await _get_active_week(session)

    # idempotency check
    existing = await session.execute(select(Order).where(Order.idempotency_key == idempotency_key))
    if existing.scalar_one_or_none() is not None:
        # return existing order
        result = await session.execute(select(Order).where(Order.idempotency_key == idempotency_key))
        return result.scalar_one()

    # price check BEFORE lock? spec says execution price is server price at processing time, and staleness check.
    # We check inside transaction as well, but we need price now.
    try:
        price, price_ts = price_cache.get_price_or_raise(symbol)
    except (PriceStale, PriceNotAvailable) as e:
        # create rejected order
        order = Order(
            user_id=user.id, week_id=week.id, asset_symbol=symbol, side=OrderSide.buy.value,
            notional_usd=notional_usd, status=OrderStatus.rejected.value,
            idempotency_key=idempotency_key, rejection_reason=str(e),
            requested_at=datetime.now(timezone.utc)
        )
        session.add(order)
        await session.flush()
        raise StalePrice(str(e))

    # asset check
    asset_result = await session.execute(select(Asset).where(Asset.symbol == symbol))
    asset = asset_result.scalar_one_or_none()
    if not asset or asset.status != "active":
        order = Order(
            user_id=user.id, week_id=week.id, asset_symbol=symbol, side=OrderSide.buy.value,
            notional_usd=notional_usd, status=OrderStatus.rejected.value,
            idempotency_key=idempotency_key, rejection_reason="Asset not found or delisted",
            requested_at=datetime.now(timezone.utc)
        )
        session.add(order)
        await session.flush()
        raise TradingError("Asset not found or delisted")

    # Lock user/week
    await lock_user_week(session, user.id, week.id)

    # Recheck price staleness inside lock (still use same price_ts)
    # If stale now, reject
    age = (datetime.now(timezone.utc) - price_ts).total_seconds()
    if age > price_cache.max_staleness:
        order = Order(
            user_id=user.id, week_id=week.id, asset_symbol=symbol, side=OrderSide.buy.value,
            notional_usd=notional_usd, status=OrderStatus.rejected.value,
            idempotency_key=idempotency_key, rejection_reason="Price stale",
            requested_at=datetime.now(timezone.utc)
        )
        session.add(order)
        await session.flush()
        raise StalePrice("Price stale inside transaction")

    balance = await get_cash_balance(session, user.id, week.id)
    if balance < notional_usd:
        order = Order(
            user_id=user.id, week_id=week.id, asset_symbol=symbol, side=OrderSide.buy.value,
            notional_usd=notional_usd, status=OrderStatus.rejected.value,
            executed_price=price, price_source_timestamp=price_ts,
            idempotency_key=idempotency_key, rejection_reason="Insufficient funds",
            requested_at=datetime.now(timezone.utc)
        )
        session.add(order)
        await session.flush()
        raise InsufficientFunds(f"Balance {balance} < {notional_usd}")

    # compute qty
    qty = (notional_usd / price).quantize(QTY_QUANT)

    # create order
    order = Order(
        user_id=user.id, week_id=week.id, asset_symbol=symbol, side=OrderSide.buy.value,
        notional_usd=notional_usd, qty=qty, status=OrderStatus.filled.value,
        executed_price=price, executed_at=datetime.now(timezone.utc),
        price_source_timestamp=price_ts, idempotency_key=idempotency_key,
        requested_at=datetime.now(timezone.utc)
    )
    session.add(order)
    await session.flush()

    # create transaction: debit cash
    # need balance_after = balance - notional
    balance_after = (balance - notional_usd).quantize(USD_QUANT)
    # idempotency for transaction is separate; use order idempotency + suffix
    tx_key = f"{idempotency_key}:tx"
    # check existing tx
    tx_exist = await session.execute(select(Transaction).where(Transaction.idempotency_key == tx_key))
    if tx_exist.scalar_one_or_none() is None:
        tx = Transaction(
            user_id=user.id, week_id=week.id, type=TransactionType.TRADE_BUY.value,
            amount=-notional_usd, balance_after=balance_after,
            ref_order_id=order.id, idempotency_key=tx_key
        )
        session.add(tx)

    # update position
    pos = await get_position(session, user.id, week.id, symbol)
    if pos is None:
        pos = Position(user_id=user.id, week_id=week.id, asset_symbol=symbol, qty=qty, avg_entry_price=price)
        session.add(pos)
    else:
        # weighted avg
        total_qty = pos.qty + qty
        if total_qty > 0:
            new_avg = (pos.qty * pos.avg_entry_price + qty * price) / total_qty
            pos.avg_entry_price = new_avg.quantize(PRICE_QUANT)
        pos.qty = total_qty.quantize(QTY_QUANT)
        pos.updated_at = datetime.now(timezone.utc)

    await session.flush()
    return order

async def execute_sell(
    session: AsyncSession,
    user: User,
    symbol: str,
    qty: Decimal | str,
    idempotency_key: str,
) -> Order:
    ensure_can_trade(user)
    week = await _get_active_week(session)

    existing = await session.execute(select(Order).where(Order.idempotency_key == idempotency_key))
    if existing.scalar_one_or_none() is not None:
        result = await session.execute(select(Order).where(Order.idempotency_key == idempotency_key))
        return result.scalar_one()

    # handle "all"
    is_all = isinstance(qty, str) and qty.lower() == "all"

    try:
        price, price_ts = price_cache.get_price_or_raise(symbol)
    except (PriceStale, PriceNotAvailable) as e:
        order = Order(
            user_id=user.id, week_id=week.id, asset_symbol=symbol, side=OrderSide.sell.value,
            qty=None, status=OrderStatus.rejected.value,
            idempotency_key=idempotency_key, rejection_reason=str(e),
            requested_at=datetime.now(timezone.utc)
        )
        session.add(order)
        await session.flush()
        raise StalePrice(str(e))

    asset_result = await session.execute(select(Asset).where(Asset.symbol == symbol))
    asset = asset_result.scalar_one_or_none()
    if not asset or asset.status != "active":
        order = Order(
            user_id=user.id, week_id=week.id, asset_symbol=symbol, side=OrderSide.sell.value,
            status=OrderStatus.rejected.value, idempotency_key=idempotency_key,
            rejection_reason="Asset not found", requested_at=datetime.now(timezone.utc)
        )
        session.add(order)
        await session.flush()
        raise TradingError("Asset not found")

    await lock_user_week(session, user.id, week.id)

    age = (datetime.now(timezone.utc) - price_ts).total_seconds()
    if age > price_cache.max_staleness:
        order = Order(
            user_id=user.id, week_id=week.id, asset_symbol=symbol, side=OrderSide.sell.value,
            status=OrderStatus.rejected.value, idempotency_key=idempotency_key,
            rejection_reason="Price stale", requested_at=datetime.now(timezone.utc)
        )
        session.add(order)
        await session.flush()
        raise StalePrice("Price stale")

    pos = await get_position(session, user.id, week.id, symbol)
    available = pos.qty if pos else Decimal("0")

    if is_all:
        sell_qty = available.quantize(QTY_QUANT)
        if sell_qty <= 0:
            order = Order(
                user_id=user.id, week_id=week.id, asset_symbol=symbol, side=OrderSide.sell.value,
                status=OrderStatus.rejected.value, idempotency_key=idempotency_key,
                rejection_reason="No position", requested_at=datetime.now(timezone.utc)
            )
            session.add(order)
            await session.flush()
            raise InsufficientPosition("No position to sell")
    else:
        sell_qty = Decimal(str(qty)).quantize(QTY_QUANT)
        if sell_qty <= 0:
            raise TradingError("Qty must be positive")
        if available < sell_qty:
            order = Order(
                user_id=user.id, week_id=week.id, asset_symbol=symbol, side=OrderSide.sell.value,
                qty=sell_qty, status=OrderStatus.rejected.value,
                executed_price=price, price_source_timestamp=price_ts,
                idempotency_key=idempotency_key, rejection_reason="Insufficient position",
                requested_at=datetime.now(timezone.utc)
            )
            session.add(order)
            await session.flush()
            raise InsufficientPosition(f"Available {available} < {sell_qty}")

    # proceeds
    proceeds = (sell_qty * price).quantize(USD_QUANT)

    order = Order(
        user_id=user.id, week_id=week.id, asset_symbol=symbol, side=OrderSide.sell.value,
        qty=sell_qty, status=OrderStatus.filled.value,
        executed_price=price, executed_at=datetime.now(timezone.utc),
        price_source_timestamp=price_ts, idempotency_key=idempotency_key,
        requested_at=datetime.now(timezone.utc)
    )
    session.add(order)
    await session.flush()

    balance = await get_cash_balance(session, user.id, week.id)
    balance_after = (balance + proceeds).quantize(USD_QUANT)
    tx_key = f"{idempotency_key}:tx"
    tx_exist = await session.execute(select(Transaction).where(Transaction.idempotency_key == tx_key))
    if tx_exist.scalar_one_or_none() is None:
        tx = Transaction(
            user_id=user.id, week_id=week.id, type=TransactionType.TRADE_SELL.value,
            amount=proceeds, balance_after=balance_after,
            ref_order_id=order.id, idempotency_key=tx_key
        )
        session.add(tx)

    # update position
    if pos:
        pos.qty = (pos.qty - sell_qty).quantize(QTY_QUANT)
        if pos.qty < 0:
            pos.qty = Decimal("0")
        pos.updated_at = datetime.now(timezone.utc)

    await session.flush()
    return order

async def get_cash_balance_for_user(session: AsyncSession, user_id: int, week_id: int) -> Decimal:
    return await get_cash_balance(session, user_id, week_id)

async def verify_balances(session: AsyncSession, user_ids: list[int] | None = None, week_id: int | None = None):
    """Hourly invariant check: compares computed sum vs stored. Returns mismatches."""
    # Since we don't store separate balance field, we just verify all balances >=0 and
    # that last transaction's balance_after matches sum.
    mismatches = []
    if week_id is not None:
        # check all users in week
        from sqlalchemy import select
        result = await session.execute(select(Transaction.user_id).where(Transaction.week_id == week_id).distinct())
        uids = [r[0] for r in result.all()]
    else:
        uids = user_ids or []
    for uid in uids:
        # get last transaction balance_after
        result = await session.execute(
            select(Transaction).where(Transaction.user_id == uid, Transaction.week_id == week_id).order_by(Transaction.id.desc()).limit(1)
        )
        last = result.scalar_one_or_none()
        computed = await get_cash_balance(session, uid, week_id)
        if last and last.balance_after != computed:
            mismatches.append((uid, computed, last.balance_after))
        if computed < 0:
            mismatches.append((uid, computed, "negative"))
    return mismatches
