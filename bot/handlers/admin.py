from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select

from bot.emojis import (
    CHART_ID,
    CHECK_ID,
    CROSS_ID,
    SIREN_ID,
    WARNING_ID,
    tg_emoji,
)
from config import settings
from db.competition_models import Competition, CompetitionStatus
from db.models import User
from db.paper_models import PaperPosition
from services.demo import create_demo_cup, seed_demo_players
from services.leaderboard import build_leaderboard
from services.metrics import snapshot as metrics_snapshot
from services.notifications import notify_competition_finished

router = Router()

TG_CHECK = tg_emoji(CHECK_ID, "✔️")
TG_WARNING = tg_emoji(WARNING_ID, "⚠️")
TG_CHART = tg_emoji(CHART_ID, "📊")
TG_SIREN = tg_emoji(SIREN_ID, "🚨")

def is_admin(telegram_id: int) -> bool:
    return telegram_id in settings.admin_ids_set

@router.message(Command("admin_stats"))
async def admin_stats(message: Message, session):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    user_count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    pos_count = (await session.execute(select(func.count()).select_from(PaperPosition))).scalar_one()
    await message.answer(f"Users: {user_count}\nPaper positions rows: {pos_count}")

@router.message(Command("admin_ban"))
async def admin_ban(message: Message, session):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Использование: /admin_ban <telegram_id> <причина>")
        return
    try:
        tid = int(parts[1])
    except ValueError:
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

@router.message(Command("admin_unban"))
async def admin_unban(message: Message, session):
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
    await message.answer(f"{TG_CHECK} Пользователь {telegram_id} разблокирован", parse_mode=ParseMode.HTML)

@router.message(Command("admin_active_competition"))
async def admin_active_competition(message: Message, session):
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
        f"{TG_CHART} {competition.name}\nID: {competition.id}\n"
        f"До: {competition.ends_at.isoformat()}\n"
        f"Баланс: ${competition.initial_balance}\nПризы: ${competition.prize_pool}",
        parse_mode=ParseMode.HTML,
    )

@router.message(Command("admin_create_demo_cup"))
async def admin_create_demo_cup(message: Message, session):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    if not settings.demo_seed_enabled:
        await message.answer(f"{TG_WARNING} DEMO_SEED_ENABLED=false: демо-функции выключены.", parse_mode=ParseMode.HTML)
        return
    try:
        competition = await create_demo_cup(session)
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        await message.answer(f"{TG_WARNING} {exc}", parse_mode=ParseMode.HTML)
        return
    await message.answer(
        f"{TG_CHECK} DEMO TRADING CUP готов\nID: {competition.id}\n"
        f"Баланс: ${competition.initial_balance}\nПризовой фонд: ${competition.prize_pool}\n"
        f"Длительность: {settings.demo_cup_duration_hours}ч\nРейтинг: ROI",
        parse_mode=ParseMode.HTML,
    )

@router.message(Command("admin_seed_demo_players"))
async def admin_seed_demo_players(message: Message, session):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    if not settings.demo_seed_enabled:
        await message.answer(f"{TG_WARNING} DEMO_SEED_ENABLED=false: демо-функции выключены.", parse_mode=ParseMode.HTML)
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
            await message.answer(f"{TG_WARNING} {exc}", parse_mode=ParseMode.HTML)
            return
    created = await seed_demo_players(session, competition.id)
    await session.commit()
    await message.answer(f"{TG_CHECK} Симулированные игроки готовы: создано новых {created}.", parse_mode=ParseMode.HTML)

@router.message(Command("admin_reconcile"))
async def admin_reconcile(message: Message, session):
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
    await message.answer(f"{TG_CHECK} Рейтинг пересчитан. Участников: {len(leaderboard)}", parse_mode=ParseMode.HTML)

@router.message(Command("admin_finish_competition"))
async def admin_finish_competition(message: Message, session):
    """Ручной триггер завершения турнира (фолбэк к фоновой задаче lifecycle)."""
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
        await message.answer(f"{TG_WARNING} Турнир не завершён: рынок временно недоступен или есть незакрытая операция.", parse_mode=ParseMode.HTML)
        return
    await message.answer(f"{TG_CHECK} Турнир завершён и рейтинг/призы зафиксированы." if finished else "Турнир уже завершён.", parse_mode=ParseMode.HTML)

@router.message(Command("admin_product_stats"))
async def admin_product_stats(message: Message, session):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    open_positions = (await session.execute(select(func.count()).select_from(PaperPosition).where(PaperPosition.status == "OPEN"))).scalar_one()
    metric_lines = "\n".join(f"{key}: {value}" for key, value in metrics_snapshot().items()) or "Пока нет событий"
    await message.answer(
        f"{TG_CHART} <b>PRODUCT STATS</b>\n\n"
        f"Пользователей: {users}\n"
        f"Открытых позиций: {open_positions}\n\n"
        f"Runtime metrics:\n{metric_lines}",
        parse_mode=ParseMode.HTML,
    )


