from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from bot.views import back_keyboard, competition_prize_lines, fmt_money, fmt_pct
from config import settings
from db.models import User
from services.competition import get_active_competition
from services.leaderboard import get_top_n, get_user_rank
from services.metrics import increment

router = Router()


def trade_button(comp_id: int | None = None) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="🚀 ТОРГОВАТЬ", callback_data="nav:trade")]]
    if comp_id and settings.bot_username:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="📣 ПОДЕЛИТЬСЯ РЕЗУЛЬТАТОМ",
                    url=f"https://t.me/{settings.bot_username}?start=competition_{comp_id}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("top"))
@router.message(F.text.in_({"🏆 Рейтинг", "🏆 TOP 10"}))
async def cmd_top(message: Message, session):
    increment("leaderboard_viewed")
    competition = await get_active_competition(session)
    if competition is None:
        await message.answer("⏳ Активного турнира сейчас нет.", reply_markup=back_keyboard("nav:home"))
        return
    top = await get_top_n(session, competition.id, 10)
    user = (await session.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one_or_none()
    user_rank = await get_user_rank(session, competition.id, user.id) if user else None
    seconds_left = max(0, int((competition.ends_at - datetime.now(timezone.utc)).total_seconds()))
    hours, remainder = divmod(seconds_left, 3600)
    minutes = remainder // 60
    text = (
        f"🏆 {competition.name}\n\n"
        f"⏱ До конца: {hours}ч {minutes}м\n"
        f"🎁 Призовой фонд: {fmt_money(competition.prize_pool)}\n\n"
        "━━━━━━━━━━━━\n\n"
    )
    medals = ["🥇", "🥈", "🥉", "4.", "5.", "6.", "7.", "8.", "9.", "10."]
    for index, entry in enumerate(top):
        participant = await session.get(User, entry["user_id"])
        if participant and participant.is_simulated:
            simulated_name = participant.username or f"DEMO_{entry['user_id']}"
            name = f"🤖 {simulated_name}"
        else:
            name = participant.username if participant and participant.username else f"User{entry['user_id']}"
        text += f"{medals[index]} {name}\n{fmt_pct(entry['roi'])}\n"
    text += f"\n🎁 Распределение\n{competition_prize_lines(competition.prize_pool)}\n\n━━━━━━━━━━━━\n\n"
    if user_rank:
        text += f"👤 ТЫ #{user_rank['rank']}\nROI: {fmt_pct(user_rank['roi'])}\n"
        if user_rank["rank"] > 10 and user_rank.get("need_for_top10") is not None:
            text += f"🎯 До TOP 10: {fmt_pct(user_rank['need_for_top10'])}\n"
        elif user_rank["rank"] > 1:
            text += "🔥 Ты в TOP 10. Продолжай защищать позицию!\n"
        else:
            text += "👑 Ты на первом месте! Защищай позицию.\n"
    else:
        text += "👤 ТЫ — пока не участвуешь\n"
    await message.answer(text, reply_markup=trade_button(competition.id))


@router.callback_query(F.data == "nav:top")
async def nav_top(callback: CallbackQuery, session):
    await cmd_top(callback.message, session)
    await callback.answer()


@router.callback_query(F.data == "go_top")
async def legacy_go_top(callback: CallbackQuery, session):
    await cmd_top(callback.message, session)
    await callback.answer()


@router.callback_query(F.data == "go_trade")
async def legacy_go_trade(callback: CallbackQuery):
    await callback.message.answer("Используй кнопку 🚀 Торговать или отправь /trade.")
    await callback.answer()
