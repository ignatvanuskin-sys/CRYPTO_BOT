from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from db.models import User

async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str | None = None) -> User:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user:
        if username and user.username != username:
            user.username = username
        return user
    user = User(telegram_id=telegram_id, username=username)
    session.add(user)
    await session.flush()
    return user

async def accept_rules(session: AsyncSession, user: User):
    if user.rules_accepted_at is None:
        user.rules_accepted_at = datetime.now(timezone.utc)

async def verify_phone(session: AsyncSession, user: User, phone_number: str):
    # phone unique constraint handled by DB
    user.phone_number = phone_number
    user.phone_verified_at = datetime.now(timezone.utc)
    await session.flush()

async def verify_phone_and_grant(session: AsyncSession, user: User, phone_number: str):
    """
    Регистрация в середине недели (gap fix).
    Выдаёт WEEKLY_GRANT сразу по верификации телефона для текущей активной недели,
    используя тот же UNIQUE (user_id, week_id) WHERE type='WEEKLY_GRANT' что и weekly_cycle.
    Идемпотентна: повторный вызов или гонка с weekly джобой не создаст дубль.
    """
    from db.models import Week, Transaction, TransactionType
    from sqlalchemy import select
    from config import settings
    from decimal import Decimal
    await verify_phone(session, user, phone_number)
    # grant for active week if not already
    result = await session.execute(select(Week).where(Week.status == "active").order_by(Week.id.desc()).limit(1))
    week = result.scalar_one_or_none()
    if week is None:
        # create first week
        week = Week(week_number=1, starts_at=datetime.now(timezone.utc), ends_at=datetime.now(timezone.utc), status="active")
        session.add(week)
        await session.flush()
    # check already granted
    existing = await session.execute(
        select(Transaction).where(Transaction.user_id == user.id, Transaction.week_id == week.id, Transaction.type == TransactionType.WEEKLY_GRANT.value)
    )
    if existing.scalar_one_or_none() is not None:
        return
    from db.repo import get_cash_balance
    balance = await get_cash_balance(session, user.id, week.id)
    amount = Decimal(settings.weekly_grant_amount)
    balance_after = (balance + amount).quantize(Decimal("0.01"))
    tx = Transaction(
        user_id=user.id, week_id=week.id, type=TransactionType.WEEKLY_GRANT.value,
        amount=amount, balance_after=balance_after, idempotency_key=f"grant:{week.id}:{user.id}"
    )
    session.add(tx)
    await session.flush()

async def ban_user(session: AsyncSession, user: User, reason: str):
    user.is_banned = True
    user.ban_reason = reason

async def unban_user(session: AsyncSession, user: User):
    user.is_banned = False
    user.ban_reason = None

def ensure_can_trade(user: User):
    if user.is_banned:
        raise PermissionError(f"User banned: {user.ban_reason}")
    if user.phone_verified_at is None:
        raise PermissionError("Phone verification required")
    if user.rules_accepted_at is None:
        raise PermissionError("Rules acceptance required")
