from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from sqlalchemy import func, select

from db.competition_models import Competition, CompetitionParticipant
from db.models import User
from db.paper_models import PaperPosition, TradingAccount
from services.bingx_market_data import get_shared_snapshot
from services.leaderboard import get_user_rank


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚀 Торговать"), KeyboardButton(text="💼 Позиции")],
            [KeyboardButton(text="🏆 Рейтинг"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="ℹ️ Как играть")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def back_keyboard(target: str = "nav:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=target)]]
    )


def fmt_money(value: Decimal | int | float | None) -> str:
    if value is None:
        return "—"
    return f"${Decimal(str(value)):,.2f}"


def fmt_pct(value: Decimal | int | float | None) -> str:
    if value is None:
        return "—"
    return f"{Decimal(str(value)):+.2f}%"


def competition_prize_lines(prize_pool: Decimal | None) -> str:
    if prize_pool == Decimal("100"):
        return "🥇 $50.00\n🥈 $25.00\n🥉 $15.00\n4–9 $1.43\n10 $1.42"
    return f"🎁 Призовой фонд: {fmt_money(prize_pool)}"


async def get_display_snapshot(session, symbol: str):
    """Read only the shared PostgreSQL market snapshot for user display."""
    try:
        return await get_shared_snapshot(session, symbol, 2000)
    except Exception:
        return None


async def competition_summary(session, competition: Competition) -> tuple[int, int]:
    participants = await session.execute(
        select(func.count()).select_from(CompetitionParticipant).where(
            CompetitionParticipant.competition_id == competition.id
        )
    )
    participant_count = int(participants.scalar_one() or 0)
    users = await session.execute(select(func.count()).select_from(User))
    return participant_count, int(users.scalar_one() or 0)


async def start_text(session, user: User, account: TradingAccount, competition: Competition | None) -> str:
    if competition is None:
        return (
            "⏳ СЛЕДУЮЩИЙ ТУРНИР СКОРО\n\n"
            "Следи за ботом — новый турнир появится автоматически."
        )
    participant_count, _ = await competition_summary(session, competition)
    rank = await get_user_rank(session, competition.id, user.id)
    seconds_left = max(0, int((competition.ends_at - datetime.now(timezone.utc)).total_seconds()))
    hours, remainder = divmod(seconds_left, 3600)
    minutes = remainder // 60
    rank_text = f"#{rank['rank']}\n{fmt_pct(rank['roi'])}" if rank else "Пока не в рейтинге"
    return (
        "🏆 CRYPTO TRADING ARENA\n\n"
        "Торгуй. Поднимайся в рейтинге. Забирай приз.\n\n"
        f"💰 Демо-баланс\n{fmt_money(account.initial_balance)}\n\n"
        f"🏆 {competition.name}\n"
        f"⏱ До завершения: {hours}ч {minutes}м\n"
        f"👥 Участников: {participant_count}\n"
        f"🎁 Призовой фонд: {fmt_money(competition.prize_pool)}\n\n"
        f"📈 Твой результат\n{rank_text}\n\n"
        f"🎁 Призы\n{competition_prize_lines(competition.prize_pool)}"
    )


async def competition_text(session, competition: Competition) -> str:
    participant_count, _ = await competition_summary(session, competition)
    seconds_left = max(0, int((competition.ends_at - datetime.now(timezone.utc)).total_seconds()))
    hours, remainder = divmod(seconds_left, 3600)
    minutes = remainder // 60
    return (
        f"🏆 {competition.name}\n\n"
        f"💰 Старт: {fmt_money(competition.initial_balance)}\n"
        f"🎁 Призовой фонд: {fmt_money(competition.prize_pool)}\n"
        f"👥 Участников: {participant_count}\n"
        f"⏱ Осталось: {hours}ч {minutes}м\n"
        f"📅 Завершение: {competition.ends_at.astimezone(timezone.utc):%d.%m.%Y %H:%M} UTC\n\n"
        "━━━━━━━━━━━━\n\n"
        f"{competition_prize_lines(competition.prize_pool)}\n\n"
        "📜 ПРАВИЛА\n"
        "• Все сделки проходят с виртуальным балансом.\n"
        "• Рейтинг считается по ROI.\n"
        "• Источник цены — BingX Perpetual."
    )
