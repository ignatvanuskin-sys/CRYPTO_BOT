from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from sqlalchemy import select
from db.models import User
from db.paper_models import TradingAccount, PaperPosition, PositionStatus
from db.competition_models import CompetitionParticipant
from services.competition import get_or_create_default_competition
from services.leaderboard import get_user_rank

router = Router()

def top_trade_buttons():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 TRADE", callback_data="go_trade")],
        [InlineKeyboardButton(text="🏆 TOP 10", callback_data="go_top")],
    ])

@router.message(Command("profile"))
@router.message(Command("positions"))
@router.message(F.text.in_({"📊 MY PROFILE", "📈 MY POSITIONS", "💼 Личный кабинет"}))
async def cmd_profile(message: Message, session):
    result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Please /start first")
        return
    acc_res = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    acc = acc_res.scalar_one_or_none()
    if not acc:
        await message.answer("No account yet, /start")
        return
    comp = await get_or_create_default_competition(session)
    rank_info = await get_user_rank(session, comp.id, user.id)
    rank = rank_info["rank"] if rank_info else "—"
    roi = f"{rank_info['roi']:+.2f}%" if rank_info else "0%"

    # stats
    q = await session.execute(select(PaperPosition).where(PaperPosition.account_id == acc.id))
    positions = q.scalars().all()
    total = len(positions)
    closed = [p for p in positions if p.status == "CLOSED"]
    wins = len([p for p in closed if p.realized_pnl > 0])
    losses = len([p for p in closed if p.realized_pnl <= 0])
    best = max((p.realized_pnl for p in closed), default=0)
    worst = min((p.realized_pnl for p in closed), default=0)

    text = f"👤 YOUR PROFILE\n\n💰 Equity ${acc.equity}\n📈 ROI {roi}\n🏆 Rank #{rank}\n\nTrades {total}\nWins {wins}\nLosses {losses}\n\nBest trade +${best}\nWorst trade ${worst}\n"
    await message.answer(text, reply_markup=top_trade_buttons())

@router.message(Command("positions"))
async def cmd_positions_list(message: Message, session):
    result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Please /start")
        return
    acc_res = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    acc = acc_res.scalar_one_or_none()
    if not acc:
        await message.answer("No account")
        return
    q = await session.execute(select(PaperPosition).where(PaperPosition.account_id == acc.id, PaperPosition.status == PositionStatus.OPEN.value).order_by(PaperPosition.opened_at.desc()))
    open_positions = q.scalars().all()
    if not open_positions:
        await message.answer("📈 OPEN POSITIONS\n\nNo open positions")
        return
    for pos in open_positions:
        # update unrealized via pricing
        from services.bingx_market_data import get_snapshot
        snap = get_snapshot(pos.symbol)
        if snap and snap.bid and snap.ask:
            price = snap.bid if pos.side == "LONG" else snap.ask  # for display, use opposite for close value
            # actually unrealized uses opposite: LONG unrealized = (bid - entry)*qty
            from services.pnl import calc_unrealized
            cur = snap.bid if pos.side == "LONG" else snap.ask
            unreal = calc_unrealized(pos.side, pos.entry_price, cur, pos.quantity)
        else:
            cur = pos.current_price
            unreal = pos.unrealized_pnl
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="CLOSE POSITION", callback_data=f"close_pos:{pos.id}")]])
        await message.answer(f"📈 {pos.symbol} {pos.side}\nEntry ${pos.entry_price}\nCurrent ${cur}\nPnL {unreal:+} \nSize {pos.quantity}", reply_markup=kb)

@router.message(Command("help"))
@router.message(F.text == "ℹ️ HOW TO PLAY")
async def cmd_help(message: Message):
    await message.answer("🎮 HOW TO PLAY\n\n1. /start → $10,000 DEMO\n2. /trade → choose BTC/ETH/SOL → LONG/SHORT → size $500 → confirm (server price)\n3. Watch PnL, TP/SL auto-close\n4. /top → leaderboard ROI\n5. /profile → stats\n\nPrices from BingX Perpetual, bid/ask, no frontend price trust.")

# main menu
@router.message(Command("start"))
async def cmd_start_new(message: Message, session):
    from sqlalchemy import select as sel
    from db.models import User
    from services.trading_account import get_or_create_trading_account
    from services.competition import join_competition, get_or_create_default_competition
    result = await session.execute(sel(User).where(User.telegram_id == message.from_user.id))
    user = result.scalar_one_or_none()
    is_new = False
    if not user:
        user = User(telegram_id=message.from_user.id, username=message.from_user.username)
        session.add(user)
        await session.flush()
        is_new = True
    acc = await get_or_create_trading_account(session, user.id)
    comp = await get_or_create_default_competition(session)
    await join_competition(session, user.id, comp.id)
    await session.commit()
    if is_new:
        text = "🎮 CRYPTO TRADING ARENA\n\nWelcome!\n\nYou have received:\n💰 $10,000 DEMO\n\nCompete for TOP 10\n🏆 Weekly prizes"
    else:
        text = "Welcome back 👋"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 START TRADING", callback_data="go_trade")],
        [InlineKeyboardButton(text="🏆 LEADERBOARD", callback_data="go_top")],
    ])
    await message.answer(text, reply_markup=kb)

@router.callback_query(F.data == "go_top")
async def cb_go_top2(callback, session):
    # reuse leaderboard
    from bot.handlers.leaderboard import cmd_top
    # create fake message
    await cmd_top(callback.message, session)
    await callback.answer()
