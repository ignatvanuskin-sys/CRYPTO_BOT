from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
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

async def verify_phone(session: AsyncSession, user: User, phone_number: str):
    # phone unique constraint handled by DB
    user.phone_number = phone_number
    user.phone_verified_at = datetime.now(timezone.utc)
    await session.flush()

def ensure_can_trade(user: User):
    if user.is_banned:
        raise PermissionError(f"User banned: {user.ban_reason}")
    if user.phone_verified_at is None:
        raise PermissionError("Phone verification required")
