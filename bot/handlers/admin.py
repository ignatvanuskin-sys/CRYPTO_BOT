from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from db.models import User, Week, LeaderboardSnapshot, Prize
from config import settings
from services.weekly_cycle import close_week, get_or_create_active_week
from decimal import Decimal

router = Router()

def is_admin(telegram_id: int) -> bool:
    return telegram_id in settings.admin_ids_set

@router.message(Command("admin_stats"))
async def admin_stats(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    user_count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    week_res = await session.execute(select(Week).order_by(Week.id.desc()).limit(1))
    week = week_res.scalar_one_or_none()
    await message.answer(f"Users: {user_count}\nActive week: {week.id if week else 'none'} status {week.status if week else ''}")

@router.message(Command("admin_review_top"))
async def admin_review_top(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    week_res = await session.execute(select(Week).order_by(Week.id.desc()).limit(1))
    week = week_res.scalar_one_or_none()
    if not week:
        await message.answer("Нет недели")
        return
    snap_res = await session.execute(select(LeaderboardSnapshot, User).join(User, LeaderboardSnapshot.user_id == User.id).where(LeaderboardSnapshot.week_id == week.id).order_by(LeaderboardSnapshot.rank).limit(settings.prize_top_n))
    lines = []
    for snap, user in snap_res.all():
        lines.append(f"{snap.rank}. {user.username} phone {user.phone_number} equity {snap.total_equity} cash {snap.cash_balance}")
    await message.answer("\n".join(lines) if lines else "Топ пуст")

@router.message(Command("admin_ban"))
async def admin_ban(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Использование: /admin_ban <telegram_id> <причина>")
        return
    try:
        tid = int(parts[1])
    except:
        await message.answer("Неверный telegram_id")
        return
    reason = parts[2] if len(parts) > 2 else "admin ban"
    result = await session.execute(select(User).where(User.telegram_id == tid))
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Юзер не найден")
        return
    user.is_banned = True
    user.ban_reason = reason
    await session.commit()
    await message.answer(f"Забанен {tid}")

@router.message(Command("admin_force_close_week"))
async def admin_force_close(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    from bot.keyboards import force_close_confirm
    await message.answer("Точно закрыть неделю? Это идемпотентно.", reply_markup=force_close_confirm())

@router.callback_query(F.data == "force_close_confirm")
async def cb_force_close_confirm(callback: CallbackQuery, session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    async with session.begin():
        week = await get_or_create_active_week(session)
        await close_week(session, week, prize_top_n=settings.prize_top_n, grant_amount=Decimal(settings.weekly_grant_amount))
    await session.commit()
    await callback.message.answer("Неделя закрыта")
    await callback.answer("OK")

@router.callback_query(F.data == "force_close_cancel")
async def cb_force_close_cancel(callback: CallbackQuery):
    await callback.answer("Отменено")
    await callback.message.answer("Отменено")
