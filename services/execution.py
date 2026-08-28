from __future__ import annotations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from db.competition_models import Execution
from decimal import Decimal

async def get_executions_for_position(session: AsyncSession, position_id: int) -> list[Execution]:
    result = await session.execute(select(Execution).where(Execution.position_id == position_id).order_by(Execution.executed_at))
    return result.scalars().all()

async def get_executions_for_user(session: AsyncSession, user_id: int, competition_id: int | None = None, limit: int = 50) -> list[Execution]:
    q = select(Execution).where(Execution.user_id == user_id)
    if competition_id:
        q = q.where(Execution.competition_id == competition_id)
    q = q.order_by(Execution.executed_at.desc()).limit(limit)
    result = await session.execute(q)
    return result.scalars().all()

# Immutable check: ensure execution not mutated
async def verify_execution_immutable(session: AsyncSession, execution_id: int, expected: dict) -> bool:
    ex = await session.get(Execution, execution_id)
    if not ex:
        return False
    for k, v in expected.items():
        if getattr(ex, k) != v:
            return False
    return True
