from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from db.paper_models import TradingAccount, Instrument, PaperPosition, PaperOrder, AccountLedger, PositionStatus, OrderStatus, LedgerType
from db.competition_models import Execution, ExecutionReason
from services.pricing import price_cache, PriceStale, PriceNotAvailable
from services.bingx_market_data import get_snapshot, is_stale, MarketDataUnavailable, MarketDataStale
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
    quantity: Decimal | None = None,
    take_profit: Decimal | None = None,
    stop_loss: Decimal | None = None,
    idempotency_key: str = "",
    notional: Decimal | None = None,
    competition_id: int | None = None,
    requested_at: datetime | None = None,
) -> PaperPosition:
    # idempotency
    if idempotency_key:
        existing = await session.execute(select(PaperOrder).where(PaperOrder.idempotency_key == idempotency_key))
        if existing.scalar_one_or_none():
            order = (await session.execute(select(PaperOrder).where(PaperOrder.idempotency_key == idempotency_key))).scalar_one()
            if order.position_id:
                pos = await session.get(PaperPosition, order.position_id)
                return pos
            raise PaperError("Duplicate order but no position")

    # competitions: resolve active if not given
    if competition_id is None:
        from db.competition_models import Competition, CompetitionStatus
        res = await session.execute(select(Competition).where(Competition.status == CompetitionStatus.ACTIVE.value).order_by(Competition.id.desc()).limit(1))
        comp = res.scalar_one_or_none()
        if comp:
            competition_id = comp.id

    if requested_at is None:
        requested_at = datetime.now(timezone.utc)

    side = side.upper()
    if side not in ("LONG", "SHORT"):
        raise PaperError("Side must be LONG/SHORT")

    # instrument
    inst = await session.get(Instrument, symbol)
    if not inst or inst.status != "active":
        # try dash variant
        alt = symbol.replace("-", "")
        inst = await session.get(Instrument, alt)
        if not inst or inst.status != "active":
            raise InvalidSymbol(f"Unknown symbol {symbol}")

    # price from backend bid/ask — canonical BINGX perpetual
    snap = get_snapshot(symbol)
    if snap is None:
        # fallback to legacy price_cache
        try:
            price, ts = price_cache.get_price_or_raise(symbol)
            # synthesize bid/ask from last
            snap_bid = snap_ask = price
            snap_ts = ts
        except:
            raise PaperError("Market data unavailable")
    else:
        max_age = settings.market_data_max_age_ms
        if is_stale(snap, max_age):
            raise PaperError("Market data stale")
        if snap.bid is None or snap.ask is None:
            raise PaperError("Market data unavailable (no bid/ask)")
        snap_bid, snap_ask = snap.bid, snap.ask
        snap_ts = snap.exchange_timestamp or snap.received_at

    # LONG OPEN = ASK, SHORT OPEN = BID (spec 5)
    if side == "LONG":
        raw_price = snap_ask if snap and snap.ask else snap_bid
    else:
        raw_price = snap_bid if snap and snap.bid else snap_ask
    if raw_price is None:
        raise PaperError("Market data unavailable")

    # slippage (0 for demo)
    slippage = Decimal(settings.paper_slippage_bps) / Decimal("10000")
    if slippage > 0:
        if side == "LONG":
            executed_price = (raw_price * (Decimal("1") + slippage)).quantize(PRICE_Q)
        else:
            executed_price = (raw_price * (Decimal("1") - slippage)).quantize(PRICE_Q)
    else:
        executed_price = raw_price.quantize(PRICE_Q)

    # quantity from notional if provided (new TASK: frontend sends notional)
    if notional is not None:
        notional_dec = Decimal(str(notional))
        if notional_dec <= 0:
            raise InvalidQuantity("Notional must be >0")
        quantity = (notional_dec / executed_price).quantize(QTY_Q)
    elif quantity is not None:
        quantity = Decimal(str(quantity)).quantize(QTY_Q)
        if quantity <= 0:
            raise InvalidQuantity("Quantity must be >0")
    else:
        raise InvalidQuantity("Quantity or notional required")

    if inst.min_quantity and quantity < inst.min_quantity:
        raise InvalidQuantity(f"Quantity < min {inst.min_quantity}")
    if inst.max_quantity and quantity > inst.max_quantity:
        raise InvalidQuantity(f"Quantity > max {inst.max_quantity}")

    _validate_tp_sl(side, executed_price, take_profit, stop_loss)

    # lock account
    await _lock_account(session, account.id)
    # refresh account from DB to get latest cash
    await session.refresh(account)

    notional = calc_notional(executed_price, quantity)
    # margin check: required = notional (no leverage)
    if notional > account.available_margin:
        order = PaperOrder(
            account_id=account.id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            executed_price=executed_price,
            status=OrderStatus.REJECTED.value,
            idempotency_key=idempotency_key or f"rej-{datetime.now(timezone.utc).timestamp()}",
            rejection_reason="Insufficient margin",
            requested_price=raw_price,
        )
        session.add(order)
        await session.flush()
        raise InsufficientMargin(f"Need {notional}, available {account.available_margin}")

    # create order + position (with competition_id)
    order = PaperOrder(
        account_id=account.id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        requested_price=raw_price,
        executed_price=executed_price,
        status=OrderStatus.FILLED.value,
        idempotency_key=idempotency_key,
        executed_at=datetime.now(timezone.utc),
    )
    session.add(order)
    await session.flush()

    position = PaperPosition(
        account_id=account.id,
        competition_id=competition_id,
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
    await session.flush()

    # immutable execution record (spec 7)
    # need user_id and competition_id for execution
    # get user_id via account
    from db.models import User as LegacyUser
    # account.user_id is the user
    user_id = account.user_id
    if competition_id is None:
        # fallback: try to find participant's competition
        from db.competition_models import CompetitionParticipant
        res = await session.execute(select(CompetitionParticipant).where(CompetitionParticipant.user_id == user_id).order_by(CompetitionParticipant.joined_at.desc()).limit(1))
        part = res.scalar_one_or_none()
        if part:
            competition_id = part.competition_id
    if competition_id is not None:
        execution = Execution(
            position_id=position.id,
            user_id=user_id,
            competition_id=competition_id,
            symbol=symbol,
            side=side,
            price_source="BINGX",
            market_type="USD_M_PERPETUAL",
            bid_price=snap_bid,
            ask_price=snap_ask,
            execution_price=executed_price,
            quantity=quantity,
            notional=notional,
            market_timestamp=snap_ts,
            requested_at=requested_at,
            executed_at=datetime.now(timezone.utc),
            execution_reason=ExecutionReason.OPEN.value,
        )
        session.add(execution)
        await session.flush()

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

    # get current price via bid/ask (spec 5: LONG CLOSE=BID, SHORT CLOSE=ASK)
    snap = get_snapshot(position.symbol)
    if snap is None:
        try:
            # fallback to legacy cache as synthesized bid/ask
            price, ts = price_cache.get_price_or_raise(position.symbol)
            snap_bid = snap_ask = price
            snap_ts = ts
        except:
            raise PaperError("Market data unavailable")
    else:
        max_age = settings.market_data_max_age_ms
        if is_stale(snap, max_age):
            raise PaperError("Market data stale")
        if snap.bid is None or snap.ask is None:
            raise PaperError("Market data unavailable (no bid/ask)")
        snap_bid, snap_ask = snap.bid, snap.ask
        snap_ts = snap.exchange_timestamp or snap.received_at

    # choose close price per side
    if position.side == "LONG":
        close_price = snap_bid if snap and snap.bid else snap_ask
    else:
        close_price = snap_ask if snap and snap.ask else snap_bid
    if close_price is None:
        raise PaperError("Market data unavailable")
    close_price = close_price.quantize(PRICE_Q)
    requested_at_close = datetime.now(timezone.utc)

    await _lock_account(session, account.id)
    await session.refresh(account)
    await session.refresh(position)
    if position.status != PositionStatus.OPEN.value:
        raise PaperError("Position already closed")

    # calc pnl
    gross = calc_pnl(position.side, position.entry_price, close_price, position.quantity)
    net = gross - position.fee_open - position.fee_close

    # create close order
    order = PaperOrder(
        account_id=account.id,
        position_id=position.id,
        symbol=position.symbol,
        side=position.side,
        quantity=position.quantity,
        requested_price=close_price,
        executed_price=close_price,
        status=OrderStatus.FILLED.value,
        reduce_only=True,
        idempotency_key=idempotency_key or f"close:{position.id}:{datetime.now(timezone.utc).timestamp()}",
        executed_at=datetime.now(timezone.utc),
    )
    session.add(order)
    await session.flush()

    # update position
    position.status = PositionStatus.CLOSED.value
    position.current_price = close_price
    position.realized_pnl = net
    position.unrealized_pnl = Decimal("0")
    position.closed_at = datetime.now(timezone.utc)
    position.updated_at = datetime.now(timezone.utc)

    # immutable execution for close
    # map reason to ExecutionReason
    reason_map = {"manual": ExecutionReason.MANUAL_CLOSE.value, "TP": ExecutionReason.TAKE_PROFIT.value, "SL": ExecutionReason.STOP_LOSS.value}
    exec_reason = reason_map.get(reason, ExecutionReason.MANUAL_CLOSE.value)
    # competition_id from position or active
    comp_id = getattr(position, 'competition_id', None)
    if comp_id is None:
        from db.competition_models import CompetitionParticipant
        res = await session.execute(select(CompetitionParticipant).where(CompetitionParticipant.user_id == account.user_id).order_by(CompetitionParticipant.joined_at.desc()).limit(1))
        part = res.scalar_one_or_none()
        if part:
            comp_id = part.competition_id
    if comp_id is not None:
        execution = Execution(
            position_id=position.id,
            user_id=account.user_id,
            competition_id=comp_id,
            symbol=position.symbol,
            side=position.side,
            price_source="BINGX",
            market_type="USD_M_PERPETUAL",
            bid_price=snap_bid,
            ask_price=snap_ask,
            execution_price=close_price,
            quantity=position.quantity,
            notional=calc_notional(close_price, position.quantity),
            market_timestamp=snap_ts,
            requested_at=requested_at_close,
            executed_at=datetime.now(timezone.utc),
            execution_reason=exec_reason,
        )
        session.add(execution)
        await session.flush()

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
