from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from db.paper_models import TradingAccount, AccountLedger, LedgerType
from config import settings

async def get_or_create_trading_account(session: AsyncSession, user_id: int) -> TradingAccount:
    result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user_id))
    acc = result.scalar_one_or_none()
    if acc:
        return acc
    # idempotent creation with INITIAL_BALANCE
    initial = Decimal(settings.initial_balance_usd)
    # try insert with savepoint for race
    try:
        async with session.begin_nested():
            acc = TradingAccount(
                user_id=user_id,
                currency="USD",
                initial_balance=initial,
                cash_balance=initial,
                equity=initial,
                margin_used=Decimal("0"),
                available_margin=initial,
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                total_pnl=Decimal("0"),
            )
            session.add(acc)
            await session.flush()
            # ledger INITIAL_BALANCE
            ledger = AccountLedger(
                account_id=acc.id,
                type=LedgerType.INITIAL_BALANCE.value,
                amount=initial,
                balance_after=initial,
                reference_type="initial",
                reference_id=str(acc.id),
                idempotency_key=f"init:{acc.user_id}:{acc.id}",
            )
            session.add(ledger)
            await session.flush()
            return acc
    except IntegrityError:
        # race: another transaction created it
        await session.rollback()
        result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user_id))
        acc = result.scalar_one()
        return acc

async def get_account_by_user(session: AsyncSession, user_id: int) -> TradingAccount | None:
    result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user_id))
    return result.scalar_one_or_none()

async def refresh_account_stats(session: AsyncSession, account: TradingAccount):
    # recalc equity = cash + unrealized
    account.equity = (account.cash_balance + account.unrealized_pnl).quantize(Decimal("0.01"))
    account.available_margin = (account.cash_balance - account.margin_used).quantize(Decimal("0.01"))
    if account.available_margin < 0:
        account.available_margin = Decimal("0")
    account.total_pnl = (account.realized_pnl + account.unrealized_pnl).quantize(Decimal("0.01"))
    account.updated_at = datetime.now(timezone.utc)
