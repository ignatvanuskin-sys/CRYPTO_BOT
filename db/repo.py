from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from db.models import Transaction, Position, User, Week, Order

async def get_cash_balance(session: AsyncSession, user_id: int, week_id: int) -> Decimal:
    result = await session.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.user_id == user_id, Transaction.week_id == week_id
        )
    )
    val = result.scalar_one()
    return Decimal(str(val))

async def get_positions(session: AsyncSession, user_id: int, week_id: int):
    result = await session.execute(
        select(Position).where(Position.user_id == user_id, Position.week_id == week_id, Position.qty > 0)
    )
    return result.scalars().all()

async def get_position(session: AsyncSession, user_id: int, week_id: int, symbol: str):
    result = await session.execute(
        select(Position).where(
            Position.user_id == user_id, Position.week_id == week_id, Position.asset_symbol == symbol
        )
    )
    return result.scalar_one_or_none()

async def lock_user_week(session: AsyncSession, user_id: int, week_id: int):
    """
    Блокировка пользователя для денежных операций.
    Дедлок-безопасность: всегда блокируется РОВНО ОДНА строка (users) в детерминированном порядке.
    - Не блокируются отдельные строки positions по разным символам — qty проверяется после блокировки users.
    - Если нужно расширить — все операции должны лочить в порядке (users.id ASC, week_id ASC), никогда в разном порядке.
    PG: SELECT ... FOR UPDATE; SQLite: no-op (т.к. нет построчных блокировок — тесты на гонку требуют реальный Postgres).
    """
    dialect = session.bind.dialect.name if session.bind else ""
    if dialect == "postgresql":
        # ровно одна строка, детерминированный порядок
        await session.execute(
            text("SELECT 1 FROM users WHERE id = :uid FOR UPDATE"), {"uid": user_id}
        )
        # weeks не лочим отдельно — избежание двух блокировок и риска дедлока.
        # Конкурентные ордера одного юзера сериализуются на users row; недельный цикл лочит weeks отдельно, но не конкурирует с ордерами за ту же строку в обратном порядке.
    else:
        # sqlite: begin immediate уже лочит всю БД; no-op. Тест на гонку на sqlite — невалиден (см. conftest).
        pass

async def verify_balance_invariant(session: AsyncSession, user_id: int, week_id: int) -> tuple[Decimal, bool]:
    """Returns (computed_balance, is_non_negative)."""
    bal = await get_cash_balance(session, user_id, week_id)
    return bal, bal >= 0

async def get_active_week(session: AsyncSession):
    result = await session.execute(select(Week).where(Week.status == "active").order_by(Week.id.desc()).limit(1))
    return result.scalar_one_or_none()

async def get_user_by_telegram(session: AsyncSession, telegram_id: int):
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()
