from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from db.paper_models import TradingAccount, Instrument, PaperPosition, PaperOrder, AccountLedger, PositionStatus, OrderStatus, LedgerType
from services.pricing import price_cache, PriceStale, PriceNotAvailable
from services.pnl import calc_pnl, calc_unrealized, calc_notional
from config import settings

QTY_Q = Decimal("0.000000000001")
PRICE_Q = Decimal("0.000000000001")

class PaperError(Exception):
    pass
class InsufficientMargin(PaperError):
    pass
class InvalidSymbol(PaperError):
    pass
class InvalidQuantity(PaperError):
    pass
class InvalidTP_SL(PaperError):
    pass

async def _lock_account(session: AsyncSession, account_id: int):
    dialect = session.bind.dialect.name if session.bind else ""
    if dialect == "postgresql":
        await session.execute(text("SELECT 1 FROM trading_accounts WHERE id = :id FOR UPDATE"), {"id": account_id})

def _validate_tp_sl(side: str, entry: Decimal, tp: Decimal | None, sl: Decimal | None):
    if tp is not None:
        if side == "LONG" and tp <= entry:
            raise InvalidTP_SL("TP must be > entry for LONG")
        if side == "SHORT" and tp >= entry:
            raise InvalidTP_SL("TP must be < entry for SHORT")
    if sl is not None:
        if side == "LONG" and sl >= entry:
            raise InvalidTP_SL("SL must be < entry for LONG")
        if side == "SHORT" and sl <= entry:
            raise InvalidTP_SL("SL must be > entry for SHORT")

async def open_position(
    session: AsyncSession,
    account: TradingAccount,
    symbol: str,
    side: str,  # LONG/SHORT
    quantity: Decimal,
    take_profit: Decimal | None = None,
    stop_loss: Decimal | None = None,
    idempotency_key: str = "",
) -> PaperPosition:
    # idempotency
    if idempotency_key:
        existing = await session.execute(select(PaperOrder).where(PaperOrder.idempotency_key == idempotency_key))
        if existing.scalar_one_or_none():
            # return associated position
            order = (await session.execute(select(PaperOrder).where(PaperOrder.idempotency_key == idempotency_key))).scalar_one()
            if order.position_id:
                pos = await session.get(PaperPosition, order.position_id)
                return pos
            raise PaperError("Duplicate order but no position")

    # validation
    quantity = Decimal(str(quantity)).quantize(QTY_Q)
    if quantity <= 0:
        raise InvalidQuantity("Quantity must be >0")
    side = side.upper()
    if side not in ("LONG", "SHORT"):
        raise PaperError("Side must be LONG/SHORT")

    # instrument
    inst = await session.get(Instrument, symbol)
    if not inst or inst.status != "active":
        raise InvalidSymbol(f"Unknown symbol {symbol}")
    if inst.min_quantity and quantity < inst.min_quantity:
        raise InvalidQuantity(f"Quantity < min {inst.min_quantity}")
    if inst.max_quantity and quantity > inst.max_quantity:
        raise InvalidQuantity(f"Quantity > max {inst.max_quantity}")

    # price from backend, not frontend
    try:
        # instruments use BTCUSDT format, price_cache uses BTC-USDT or BTCUSDT? normalize
        # try both
        price, ts = price_cache.get_price_or_raise(symbol)
    except (PriceStale, PriceNotAvailable):
        # try dash variant
        dash = symbol.replace("USDT", "-USDT").replace("--", "-")
        # fallback try without dash logic
        for cand in [symbol, symbol.replace("USDT", "-USDT"), symbol.replace("-", "")]:
            try:
                price, ts = price_cache.get_price_or_raise(cand)
                break
            except:
                continue
        else:
            raise PaperError("Market data unavailable")
    # slippage
    slippage = Decimal(settings.paper_slippage_bps) / Decimal("10000")
    if side == "LONG":
        executed_price = (price * (Decimal("1") + slippage)).quantize(PRICE_Q)
    else:
        executed_price = (price * (Decimal("1") - slippage)).quantize(PRICE_Q)

    _validate_tp_sl(side, executed_price, take_profit, stop_loss)

    # lock account
    await _lock_account(session, account.id)
    # refresh account from DB to get latest cash
    await session.refresh(account)

    notional = calc_notional(executed_price, quantity)
    # margin check: required = notional (no leverage)
    if notional > account.available_margin:
        # create rejected order
        order = PaperOrder(
            account_id=account.id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            executed_price=executed_price,
            status=OrderStatus.REJECTED.value,
            idempotency_key=idempotency_key or f"rej-{datetime.now(timezone.utc).timestamp()}",
            rejection_reason="Insufficient margin",
            requested_price=price,
        )
        session.add(order)
        await session.flush()
        raise InsufficientMargin(f"Need {notional}, available {account.available_margin}")

    # create order + position
    order = PaperOrder(
        account_id=account.id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        requested_price=price,
        executed_price=executed_price,
        status=OrderStatus.FILLED.value,
        idempotency_key=idempotency_key,
        executed_at=datetime.now(timezone.utc),
    )
    session.add(order)
    await session.flush()

    position = PaperPosition(
        account_id=account.id,
        symbol=symbol,
        side=side,
        status=PositionStatus.OPEN.value,
        quantity=quantity,
        entry_price=executed_price,
        current_price=executed_price,
        notional=notional,
        take_profit=take_profit,
        stop_loss=stop_loss,
        unrealized_pnl=Decimal("0"),
        opened_at=datetime.now(timezone.utc),
    )
    session.add(position)
    await session.flush()
    order.position_id = position.id

    # ledger TRADE_OPEN: deduct notional? For paper, margin_used increases, cash decreases? Spec: balance vs equity.
    # Simplistic: cash_balance -= notional, margin_used += notional
    # But then equity stays same (cash + unrealized). We'll track.
    account.cash_balance = (account.cash_balance - notional).quantize(Decimal("0.01"))
    account.margin_used = (account.margin_used + notional).quantize(Decimal("0.01"))
    account.available_margin = (account.cash_balance - account.margin_used + account.margin_used)  # hack: available = cash - margin_used? Actually cash already deducted, so available = cash - margin_used? Keep simple: available = cash
    # Simpler: available = cash_balance (since margin_used equals notional locked, but we already deducted)
    # For no leverage, available = cash_balance
    account.available_margin = account.cash_balance
    account.equity = (account.cash_balance + account.unrealized_pnl).quantize(Decimal("0.01"))

    # ledger
    ledger = AccountLedger(
        account_id=account.id,
        type=LedgerType.TRADE_OPEN.value,
        amount=-notional,
        balance_after=account.cash_balance,
        reference_type="position",
        reference_id=str(position.id),
        idempotency_key=f"{idempotency_key}:ledger" if idempotency_key else f"open:{position.id}",
    )
    session.add(ledger)

    # update account stats
    from services.trading_account import refresh_account_stats
    await refresh_account_stats(session, account)

    await session.flush()
    return position

async def close_position(
    session: AsyncSession,
    position: PaperPosition,
    account: TradingAccount,
    idempotency_key: str = "",
    reason: str = "manual",
) -> tuple[PaperPosition, Decimal]:
    if position.status != PositionStatus.OPEN.value:
        raise PaperError("Position not open")

    # idempotency for close
    if idempotency_key:
        existing = await session.execute(select(PaperOrder).where(PaperOrder.idempotency_key == idempotency_key))
        if existing.scalar_one_or_none():
            return position, position.realized_pnl

    # get current price
    try:
        price, _ = price_cache.get_price_or_raise(position.symbol)
    except:
        # try dash variant
        for cand in [position.symbol, position.symbol.replace("USDT", "-USDT")]:
            try:
                price, _ = price_cache.get_price_or_raise(cand)
                break
            except:
                continue
        else:
            raise PaperError("Market data unavailable")

    await _lock_account(session, account.id)
    await session.refresh(account)
    await session.refresh(position)
    if position.status != PositionStatus.OPEN.value:
        raise PaperError("Position already closed")

    # calc pnl
    gross = calc_pnl(position.side, position.entry_price, price, position.quantity)
    net = gross - position.fee_open - position.fee_close

    # create close order
    order = PaperOrder(
        account_id=account.id,
        position_id=position.id,
        symbol=position.symbol,
        side=position.side,
        quantity=position.quantity,
        requested_price=price,
        executed_price=price,
        status=OrderStatus.FILLED.value,
        reduce_only=True,
        idempotency_key=idempotency_key or f"close:{position.id}:{datetime.now(timezone.utc).timestamp()}",
        executed_at=datetime.now(timezone.utc),
    )
    session.add(order)
    await session.flush()

    # update position
    position.status = PositionStatus.CLOSED.value
    position.current_price = price
    position.realized_pnl = net
    position.unrealized_pnl = Decimal("0")
    position.closed_at = datetime.now(timezone.utc)
    position.updated_at = datetime.now(timezone.utc)

    # ledger: return notional + pnl
    # cash was deducted at open by notional, now we return notional + pnl
    return_amount = (position.notional + net).quantize(Decimal("0.01"))
    account.cash_balance = (account.cash_balance + return_amount).quantize(Decimal("0.01"))
    account.margin_used = (account.margin_used - position.notional).quantize(Decimal("0.01"))
    if account.margin_used < 0:
        account.margin_used = Decimal("0")
    account.realized_pnl = (account.realized_pnl + net).quantize(Decimal("0.01"))
    # unrealized will be recalculated elsewhere
    ledger = AccountLedger(
        account_id=account.id,
        type=LedgerType.TRADE_CLOSE.value,
        amount=return_amount,
        balance_after=account.cash_balance,
        reference_type="position",
        reference_id=str(position.id),
        idempotency_key=f"{idempotency_key}:ledger" if idempotency_key else f"close-ledger:{position.id}",
    )
    session.add(ledger)

    from services.trading_account import refresh_account_stats
    # need to recalc unrealized from other open positions
    # for now just update equity
    account.equity = (account.cash_balance + account.unrealized_pnl).quantize(Decimal("0.01"))
    await refresh_account_stats(session, account)
    await session.flush()
    return position, net
