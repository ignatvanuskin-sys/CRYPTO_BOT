from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from db.paper_models import TradingAccount, Instrument, PaperPosition, PaperOrder, AccountLedger, PositionStatus, OrderStatus, LedgerType
from db.competition_models import Execution, ExecutionReason
from services.bingx_market_data import (
    get_execution_snapshot,
    MarketDataUnavailable,
    MarketDataStale,
    MarketDataInvalid,
)
from services.pnl import calc_pnl, calc_unrealized, calc_notional
from config import settings
from services.metrics import increment

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
    if tp is not None and not tp.is_finite():
        raise InvalidTP_SL("TP must be finite")
    if sl is not None and not sl.is_finite():
        raise InvalidTP_SL("SL must be finite")
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

async def _resolve_idempotent_position(
    session: AsyncSession,
    idempotency_key: str,
    account: TradingAccount,
    symbol: str,
    side: str,
) -> PaperPosition:
    """Collapse a duplicate-key conflict to the canonical existing result.

    Used after a concurrent INSERT raised IntegrityError on the unique
    idempotency_key. Guarantees 'same key = same result' even when two requests
    race with the same key for different accounts/positions.
    """
    existing_order = (
        await session.execute(select(PaperOrder).where(PaperOrder.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if existing_order is None:
        raise PaperError("Idempotency key conflict")
    if existing_order.account_id != account.id or existing_order.symbol != symbol or existing_order.side != side.upper():
        raise PaperError("Idempotency key already used for another request")
    if existing_order.status == OrderStatus.REJECTED.value:
        raise InsufficientMargin(existing_order.rejection_reason or "Insufficient margin")
    position = await session.get(PaperPosition, existing_order.position_id)
    return position


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
    leverage: Decimal | int = Decimal("1"),
) -> PaperPosition:
    if not idempotency_key:
        raise PaperError("Idempotency-Key required")

    # idempotency
    if idempotency_key:
        existing_order = (await session.execute(select(PaperOrder).where(PaperOrder.idempotency_key == idempotency_key))).scalar_one_or_none()
        if existing_order is not None:
            increment("idempotency_hit")
            if existing_order.account_id != account.id or existing_order.symbol != symbol or existing_order.side != side.upper():
                raise PaperError("Idempotency key already used for another request")
            if existing_order.status == OrderStatus.REJECTED.value:
                raise InsufficientMargin(existing_order.rejection_reason or "Insufficient margin")
            if existing_order.position_id:
                pos = await session.get(PaperPosition, existing_order.position_id)
                return pos
            raise PaperError("Duplicate order but no position")

    # Every paper open must belong to a currently active, started, non-expired cup.
    from db.competition_models import Competition, CompetitionStatus

    now = datetime.now(timezone.utc)
    if competition_id is None:
        res = await session.execute(
            select(Competition)
            .where(
                Competition.status == CompetitionStatus.ACTIVE.value,
                Competition.starts_at <= now,
                Competition.ends_at > now,
            )
            .order_by(Competition.id.desc())
            .limit(1)
        )
        comp = res.scalar_one_or_none()
        if comp:
            competition_id = comp.id
        else:
            raise PaperError("Competition ended")
    else:
        comp = await session.get(Competition, competition_id)
        if (
            not comp
            or comp.status != CompetitionStatus.ACTIVE.value
            or comp.starts_at > now
            or comp.ends_at <= now
        ):
            raise PaperError("Competition ended")

    if requested_at is None:
        requested_at = datetime.now(timezone.utc)

    side = side.strip().upper()
    if side not in ("LONG", "SHORT"):
        raise PaperError("Side must be LONG/SHORT")

    leverage = Decimal(str(leverage))
    if not leverage.is_finite() or leverage < 1 or leverage > 300:
        raise PaperError("Leverage must be a finite number between 1 and 300")
    # Per-instrument cap (if instrument has max_leverage, enforce it)
    # Note: instrument is fetched later; we will re-check after instrument lookup

    # instrument
    inst = await session.get(Instrument, symbol)
    if not inst or inst.status != "active":
        # try dash variant
        alt = symbol.replace("-", "")
        inst = await session.get(Instrument, alt)
        if not inst or inst.status != "active":
            raise InvalidSymbol(f"Unknown symbol {symbol}")

    # Price comes from the shared authoritative BingX perpetual snapshot.
    # SQLite-only cache fallback exists for deterministic local unit tests.
    try:
        snap = await get_execution_snapshot(session, symbol, settings.market_data_max_age_ms)
    except MarketDataStale:
        raise PaperError("Market data stale")
    except (MarketDataUnavailable, MarketDataInvalid):
        raise PaperError("Market data unavailable")
    snap_bid, snap_ask = snap.bid, snap.ask
    snap_ts = snap.exchange_timestamp

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
        if not notional_dec.is_finite() or notional_dec <= 0:
            raise InvalidQuantity("Notional must be >0")
        quantity = (notional_dec / executed_price).quantize(QTY_Q)
    elif quantity is not None:
        quantity = Decimal(str(quantity))
        if not quantity.is_finite() or quantity <= 0:
            raise InvalidQuantity("Quantity must be a finite positive number")
        quantity = quantity.quantize(QTY_Q)
        if quantity <= 0:
            raise InvalidQuantity("Quantity must be >0")
    else:
        raise InvalidQuantity("Quantity or notional required")

    if inst.min_quantity and quantity < inst.min_quantity:
        raise InvalidQuantity(f"Quantity < min {inst.min_quantity}")
    if inst.max_quantity and quantity > inst.max_quantity:
        raise InvalidQuantity(f"Quantity > max {inst.max_quantity}")
    # Per-instrument max leverage (enforces real BingX tiers)
    max_lev = getattr(inst, "max_leverage", None)
    if max_lev is not None and leverage > Decimal(str(max_lev)):
        raise PaperError(f"Max leverage for {symbol} is {max_lev}x")

    _validate_tp_sl(side, executed_price, take_profit, stop_loss)

    # lock account
    await _lock_account(session, account.id)
    # Re-check idempotency after the account lock. Two requests can both miss
    # the initial lookup; the second must observe the committed winner before
    # inserting another order with the same key.
    await session.refresh(account)
    existing_after_lock = (
        await session.execute(
            select(PaperOrder).where(PaperOrder.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing_after_lock is not None:
        increment("idempotency_hit")
        if existing_after_lock.account_id != account.id or existing_after_lock.symbol != symbol or existing_after_lock.side != side:
            raise PaperError("Idempotency key already used for another request")
        if existing_after_lock.status == OrderStatus.REJECTED.value:
            raise InsufficientMargin(existing_after_lock.rejection_reason or "Insufficient margin")
        existing_position = await session.get(PaperPosition, existing_after_lock.position_id)
        return existing_position

    notional = calc_notional(executed_price, quantity)
    # margin check: required margin = notional / leverage
    required_margin = (notional / leverage).quantize(Decimal("0.01"))
    if required_margin > account.available_margin:
        try:
            async with session.begin_nested():
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
        except IntegrityError:
            return await _resolve_idempotent_position(session, idempotency_key, account, symbol, side)
        raise InsufficientMargin(f"Need {required_margin}, available {account.available_margin}")

    # create order + position (with competition_id). The inserts run inside a
    # savepoint so a concurrent duplicate idempotency key collapses to the
    # existing position instead of raising a raw IntegrityError.
    try:
        async with session.begin_nested():
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
                leverage=leverage,
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
            user_id = account.user_id
            if competition_id is None:
                from db.competition_models import CompetitionParticipant

                res = await session.execute(
                    select(CompetitionParticipant)
                    .where(CompetitionParticipant.user_id == user_id)
                    .order_by(CompetitionParticipant.joined_at.desc())
                    .limit(1)
                )
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
    except IntegrityError:
        return await _resolve_idempotent_position(session, idempotency_key, account, symbol, side)

    # Reserve the required margin in the paper account. `refresh_account_stats`
    # reconciles equity and available margin from this state.
    account.cash_balance = (account.cash_balance - required_margin).quantize(Decimal("0.01"))
    account.margin_used = (account.margin_used + required_margin).quantize(Decimal("0.01"))

    # ledger
    ledger = AccountLedger(
        account_id=account.id,
        type=LedgerType.TRADE_OPEN.value,
        amount=-required_margin,
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
    increment("trade_opened")
    return position

async def close_position(
    session: AsyncSession,
    position: PaperPosition,
    account: TradingAccount,
    idempotency_key: str = "",
    reason: str = "manual",
) -> tuple[PaperPosition, Decimal]:
    if not idempotency_key:
        raise PaperError("Idempotency-Key required")

    # Check the retry key before the status guard so a safe retry returns the
    # original result instead of creating or attempting a second close.
    existing_order = (await session.execute(select(PaperOrder).where(PaperOrder.idempotency_key == idempotency_key))).scalar_one_or_none()
    if existing_order is not None:
        increment("idempotency_hit")
        if existing_order.account_id != account.id or existing_order.position_id != position.id:
            raise PaperError("Idempotency key already used for another request")
        return position, position.realized_pnl
    if position.status != PositionStatus.OPEN.value:
        increment("double_close_prevented")
        raise PaperError("Position not open")

    # Close price also comes from the shared authoritative snapshot.
    try:
        snap = await get_execution_snapshot(session, position.symbol, settings.market_data_max_age_ms)
    except MarketDataStale:
        raise PaperError("Market data stale")
    except (MarketDataUnavailable, MarketDataInvalid):
        raise PaperError("Market data unavailable")
    snap_bid, snap_ask = snap.bid, snap.ask
    snap_ts = snap.exchange_timestamp

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

    # create close order (savepoint-guarded against a concurrent duplicate key)
    try:
        async with session.begin_nested():
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
    except IntegrityError:
        existing_order = (
            await session.execute(select(PaperOrder).where(PaperOrder.idempotency_key == idempotency_key))
        ).scalar_one_or_none()
        if existing_order is not None and existing_order.account_id == account.id and existing_order.position_id == position.id:
            return position, position.realized_pnl
        raise

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

    # ledger: return margin + pnl
    # cash was deducted at open by the required margin (notional / leverage),
    # now we return that margin plus the realized PnL.
    returned_margin = (position.notional / Decimal(str(position.leverage or 1))).quantize(Decimal("0.01"))
    return_amount = (returned_margin + net).quantize(Decimal("0.01"))
    account.cash_balance = (account.cash_balance + return_amount).quantize(Decimal("0.01"))
    account.margin_used = (account.margin_used - returned_margin).quantize(Decimal("0.01"))
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
    # Reconcile remaining open-position unrealized PnL and equity.
    await refresh_account_stats(session, account)
    await session.flush()
    increment("trade_closed")
    return position, net
