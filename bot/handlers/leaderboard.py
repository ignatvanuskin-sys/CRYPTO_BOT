from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from sqlalchemy import select
from db.models import User
from db.competition_models import Competition
from services.competition import get_or_create_default_competition
from services.leaderboard import build_leaderboard, get_top_n, get_user_rank
from services.bingx_market_data import get_snapshot

router = Router()

def trade_button():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 TRADE", callback_data="go_trade")]])

@router.message(Command("top"))
@router.message(F.text == "🏆 TOP 10")
async def cmd_top(message: Message, session):
    comp = await get_or_create_default_competition(session)
    top = await get_top_n(session, comp.id, 10)
    result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user = result.scalar_one_or_none()
    user_rank = None
    if user:
        user_rank = await get_user_rank(session, comp.id, user.id)

    # time left
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    delta = comp.ends_at - now
    days = delta.days
    hours = delta.seconds // 3600

    text = f"🏆 WEEKLY TRADING CUP\n\nEnds in: {days}d {hours}h\n\n"
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    for i, entry in enumerate(top):
        # get username
        u = await session.get(User, entry["user_id"])
        name = u.username or f"User{entry['user_id']}" if u else f"User{entry['user_id']}"
        roi = f"{entry['roi']:+.1f}%"
        text += f"{medals[i]} {name} {roi}\n"
    text += "\n────────────\n\n"
    if user_rank:
        text += f"YOU\n#{user_rank['rank']}  {user_rank['roi']:+.2f}%\n"
        if user_rank['rank'] > 10 and user_rank.get('need_for_top10') is not None:
            text += f"\nNeed {user_rank['need_for_top10']:+.2f}% for TOP 10\n"
    else:
        text += "YOU — not ranked yet\n"

    await message.answer(text, reply_markup=trade_button())

@router.callback_query(F.data == "go_trade")
async def cb_go_trade(callback: CallbackQuery):
    await callback.message.answer("Use /trade to select asset")
    await callback.answer()
