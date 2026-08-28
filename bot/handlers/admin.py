from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from db.models import User, Week, LeaderboardSnapshot, Prize
from db.competition_models import Competition, CompetitionStatus
from db.paper_models import PaperPosition
from config import settings
from services.competition import finish_competition
from services.demo import create_demo_cup, seed_demo_players
from services.leaderboard import build_leaderboard
from services.metrics import increment, snapshot as metrics_snapshot
from services.notifications import notify_competition_finished
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
    if settings.trading_mode == "paper":
        await message.answer("В paper-режиме используй /top и /admin_reconcile.")
        return
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
    if settings.trading_mode == "paper":
        await message.answer("В paper-режиме эта legacy-команда отключена.")
        return
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    from bot.keyboards import force_close_confirm
    await message.answer("Точно закрыть неделю? Это идемпотентно.", reply_markup=force_close_confirm())

@router.callback_query(F.data == "force_close_confirm")
async def cb_force_close_confirm(callback: CallbackQuery, session: AsyncSession):
    if settings.trading_mode == "paper":
        await callback.answer("Legacy-команда отключена в paper-режиме", show_alert=True)
        return
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


@router.message(Command("admin_active_competition"))
async def admin_active_competition(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    result = await session.execute(
        select(Competition)
        .where(Competition.status == CompetitionStatus.ACTIVE.value)
        .order_by(Competition.id.desc())
        .limit(1)
    )
    competition = result.scalar_one_or_none()
    if not competition:
        await message.answer("Активного турнира нет")
        return
    await message.answer(
        f"🎮 {competition.name}\nID: {competition.id}\n"
        f"До: {competition.ends_at.isoformat()}\n"
        f"Баланс: ${competition.initial_balance}\nПризы: ${competition.prize_pool}"
    )


@router.message(Command("admin_create_demo_cup"))
async def admin_create_demo_cup(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    if not settings.demo_seed_enabled:
        await message.answer("⚠️ DEMO_SEED_ENABLED=false: демо-функции выключены.")
        return
    try:
        competition = await create_demo_cup(session)
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        await message.answer(f"⚠️ {exc}")
        return
    await message.answer(
        f"✅ DEMO TRADING CUP готов\nID: {competition.id}\n"
        f"Баланс: $10,000\nПризовой фонд: ${competition.prize_pool}\n"
        f"Длительность: 24ч\nАктивы: BTC, ETH, SOL\nРейтинг: ROI"
    )


@router.message(Command("admin_seed_demo_players"))
async def admin_seed_demo_players(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    if not settings.demo_seed_enabled:
        await message.answer("⚠️ DEMO_SEED_ENABLED=false: демо-функции выключены.")
        return
    result = await session.execute(
        select(Competition)
        .where(
            Competition.name == "DEMO TRADING CUP",
            Competition.status == CompetitionStatus.ACTIVE.value,
        )
        .order_by(Competition.id.desc())
        .limit(1)
    )
    competition = result.scalar_one_or_none()
    if competition is None:
        try:
            competition = await create_demo_cup(session)
        except ValueError as exc:
            await session.rollback()
            await message.answer(f"⚠️ {exc}")
            return
    created = await seed_demo_players(session, competition.id)
    await session.commit()
    await message.answer(f"✅ Симулированные игроки готовы: создано новых {created}.\nМетки: 🤖 DEMO_01 …")


@router.message(Command("admin_reconcile"))
async def admin_reconcile(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    result = await session.execute(
        select(Competition)
        .where(Competition.status == CompetitionStatus.ACTIVE.value)
        .order_by(Competition.id.desc())
        .limit(1)
    )
    competition = result.scalar_one_or_none()
    if not competition:
        await message.answer("Активного турнира нет")
        return
    leaderboard = await build_leaderboard(session, competition.id)
    await session.commit()
    await message.answer(f"✅ Рейтинг пересчитан. Участников: {len(leaderboard)}")


@router.message(Command("admin_finish_competition"))
async def admin_finish_competition(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    result = await session.execute(
        select(Competition)
        .where(Competition.status == CompetitionStatus.ACTIVE.value)
        .order_by(Competition.id.desc())
        .limit(1)
    )
    competition = result.scalar_one_or_none()
    if not competition:
        await message.answer("Активного турнира нет")
        return
    from workers.competition_lifecycle import finalize_competition_session
    try:
        finished = await finalize_competition_session(session, competition.id)
        await session.commit()
        if finished and session.bind is not None:
            await notify_competition_finished(session.bind, competition.id)
    except Exception:
        await session.rollback()
        await message.answer("⚠️ Турнир не завершён: рынок временно недоступен или есть незакрытая операция.")
        return
    await message.answer("✅ Турнир завершён и рейтинг/призы зафиксированы." if finished else "Турнир уже завершён.")


@router.message(Command("admin_product_stats"))
async def admin_product_stats(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    open_positions = (await session.execute(select(func.count()).select_from(PaperPosition).where(PaperPosition.status == "OPEN"))).scalar_one()
    metric_lines = "\n".join(f"{key}: {value}" for key, value in metrics_snapshot().items()) or "Пока нет событий"
    await message.answer(
        "📊 PRODUCT STATS\n\n"
        f"Пользователей: {users}\n"
        f"Открытых позиций: {open_positions}\n\n"
        "Runtime metrics (сбрасываются после restart):\n"
        f"{metric_lines}"
    )


@router.message(Command("admin_unban"))
async def admin_unban(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().lstrip("-").isdigit():
        await message.answer("Использование: /admin_unban <telegram_id>")
        return
    telegram_id = int(parts[1])
    user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    if not user:
        await message.answer("Пользователь не найден")
        return
    user.is_banned = False
    user.ban_reason = None
    await session.commit()
    await message.answer(f"✅ Пользователь {telegram_id} разблокирован")
