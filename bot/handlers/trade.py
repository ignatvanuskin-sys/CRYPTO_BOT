from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from sqlalchemy import select
from decimal import Decimal
from datetime import datetime, timezone
from db.models import User
from db.paper_models import TradingAccount, PaperPosition, PositionStatus
from db.competition_models import Competition, CompetitionParticipant
from services.trading_account import get_or_create_trading_account
from services.competition import get_or_create_default_competition, join_competition, update_participant_equity
from services.paper_adapter import open_position, close_position, PaperError
from services.bingx_market_data import get_snapshot
from config import settings

router = Router()

# simple in-memory state for trade flow: user_id -> {symbol, side, notional, tp, sl}
trade_state: dict[int, dict] = {}

def asset_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="BTC", callback_data="asset:BTCUSDT"), InlineKeyboardButton(text="ETH", callback_data="asset:ETHUSDT"), InlineKeyboardButton(text="SOL", callback_data="asset:SOLUSDT")],
    ])

def side_keyboard(symbol: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 LONG", callback_data=f"side:{symbol}:LONG"), InlineKeyboardButton(text="🔴 SHORT", callback_data=f"side:{symbol}:SHORT")],
    ])

def size_keyboard(symbol: str, side: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="$100", callback_data=f"size:{symbol}:{side}:100"), InlineKeyboardButton(text="$500", callback_data=f"size:{symbol}:{side}:500"), InlineKeyboardButton(text="$1000", callback_data=f"size:{symbol}:{side}:1000")],
        [InlineKeyboardButton(text="✏️ Custom", callback_data=f"size_custom:{symbol}:{side}")],
    ])

def confirm_keyboard(symbol: str, side: str, notional: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ CONFIRM", callback_data=f"confirm:{symbol}:{side}:{notional}")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data="cancel_trade")],
    ])

def tp_sl_keyboard(symbol: str, side: str, notional: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Set TP/SL", callback_data=f"tp_sl:{symbol}:{side}:{notional}")],
        [InlineKeyboardButton(text="Skip TP/SL", callback_data=f"confirm:{symbol}:{side}:{notional}")],
    ])

@router.message(Command("trade"))
async def cmd_trade(message: Message):
    await message.answer("📈 SELECT ASSET", reply_markup=asset_keyboard())

@router.callback_query(F.data.startswith("asset:"))
async def cb_asset(callback: CallbackQuery):
    symbol = callback.data.split(":")[1]
    await callback.message.edit_text(f"🟣 {symbol}\n\nSelect side:", reply_markup=side_keyboard(symbol))
    await callback.answer()

@router.callback_query(F.data.startswith("side:"))
async def cb_side(callback: CallbackQuery):
    _, symbol, side = callback.data.split(":")
    trade_state[callback.from_user.id] = {"symbol": symbol, "side": side}
    # show price
    snap = get_snapshot(symbol)
    price_text = f"Ask {snap.ask} / Bid {snap.bid}" if snap and snap.bid and snap.ask else "Market data loading..."
    await callback.message.edit_text(f"🟣 {symbol} — {side}\n{price_text}\n\nSelect position size:", reply_markup=size_keyboard(symbol, side))
    await callback.answer()

@router.callback_query(F.data.startswith("size:"))
async def cb_size(callback: CallbackQuery, session):
    _, symbol, side, notional = callback.data.split(":")
    trade_state[callback.from_user.id] = {"symbol": symbol, "side": side, "notional": notional}
    snap = get_snapshot(symbol)
    ask = snap.ask if snap else "?"
    bid = snap.bid if snap else "?"
    price = ask if side == "LONG" else bid
    await callback.message.edit_text(f"⚠️ CONFIRM TRADE\n\n{symbol}\n{side}\nSize: ${notional}\nEstimated entry: ${price}\n\nTP/SL optional:", reply_markup=tp_sl_keyboard(symbol, side, notional))
    await callback.answer()

@router.callback_query(F.data.startswith("size_custom:"))
async def cb_size_custom(callback: CallbackQuery):
    _, symbol, side = callback.data.split(":")
    trade_state[callback.from_user.id] = {"symbol": symbol, "side": side, "awaiting": "notional"}
    await callback.message.edit_text(f"Enter position size in USD (e.g., 500) for {symbol} {side}:")
    await callback.answer()

@router.message(F.text.regexp(r"^\d+(\.\d+)?$"))
async def handle_custom_size(message: Message):
    state = trade_state.get(message.from_user.id)
    if not state or state.get("awaiting") != "notional":
        return
    notional = message.text.strip()
    symbol = state["symbol"]
    side = state["side"]
    trade_state[message.from_user.id] = {"symbol": symbol, "side": side, "notional": notional}
    snap = get_snapshot(symbol)
    ask = snap.ask if snap else "?"
    bid = snap.bid if snap else "?"
    price = ask if side == "LONG" else bid
    await message.answer(f"⚠️ CONFIRM TRADE\n\n{symbol}\n{side}\nSize: ${notional}\nEstimated entry: ${price}", reply_markup=tp_sl_keyboard(symbol, side, notional))

@router.callback_query(F.data.startswith("tp_sl:"))
async def cb_tp_sl(callback: CallbackQuery):
    _, symbol, side, notional = callback.data.split(":")
    trade_state[callback.from_user.id] = {"symbol": symbol, "side": side, "notional": notional, "awaiting": "tp_sl"}
    await callback.message.edit_text(f"Enter TP and SL as `TP SL` e.g., `195 175` for {symbol} {side}, or `skip`:")
    await callback.answer()

@router.message(F.text.regexp(r"^(skip|\d+.*\d+.*)$"))
async def handle_tp_sl(message: Message, session):
    # this will also catch notional, but we check awaiting
    state = trade_state.get(message.from_user.id)
    if not state or state.get("awaiting") != "tp_sl":
        return
    text = message.text.strip()
    symbol = state["symbol"]
    side = state["side"]
    notional = state["notional"]
    tp = sl = None
    if text.lower() != "skip":
        parts = text.split()
        if len(parts) >= 2:
            try:
                tp = Decimal(parts[0])
                sl = Decimal(parts[1])
            except:
                await message.answer("Invalid TP/SL, use `195 175` or `skip`")
                return
    # store and show confirm
    trade_state[message.from_user.id] = {"symbol": symbol, "side": side, "notional": notional, "tp": tp, "sl": sl}
    snap = get_snapshot(symbol)
    ask = snap.ask if snap else "?"
    bid = snap.bid if snap else "?"
    price = ask if side == "LONG" else bid
    await message.answer(f"⚠️ CONFIRM TRADE\n\n{symbol} {side}\nSize: ${notional}\nEntry: ${price}\nTP: {tp or '—'}\nSL: {sl or '—'}", reply_markup=confirm_keyboard(symbol, side, notional))

@router.callback_query(F.data.startswith("confirm:"))
async def cb_confirm(callback: CallbackQuery, session):
    _, symbol, side, notional = callback.data.split(":")
    state = trade_state.get(callback.from_user.id, {})
    tp = state.get("tp")
    sl = state.get("sl")
    # get user and account
    result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return
    acc_res = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    acc = acc_res.scalar_one_or_none()
    if not acc:
        from services.trading_account import get_or_create_trading_account
        acc = await get_or_create_trading_account(session, user.id)
        await session.flush()
        # join competition
        from services.competition import join_competition
        comp = await get_or_create_default_competition(session)
        await join_competition(session, user.id, comp.id)
        await session.commit()
        # re-fetch
        acc_res = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
        acc = acc_res.scalar_one()
    # ensure competition participant
    from services.competition import get_or_create_default_competition, join_competition
    comp = await get_or_create_default_competition(session)
    await join_competition(session, user.id, comp.id)
    await session.flush()

    # idempotency key from callback id
    idem = f"tg:{callback.id}"
    requested_at = datetime.now(timezone.utc)
    try:
        pos = await open_position(session, acc, symbol, side, quantity=None, take_profit=tp, stop_loss=sl, idempotency_key=idem, notional=Decimal(notional), competition_id=comp.id, requested_at=requested_at)
        await session.commit()
        # update participant equity
        await update_participant_equity(session, user.id, comp.id)
        await session.commit()
        await callback.message.edit_text(f"🟢 POSITION OPENED\n\n{symbol} {side}\nEntry: {pos.entry_price}\nSize: ${notional}\nTP: {tp or '—'} SL: {sl or '—'}\n\nCurrent PnL: $0.00")
        await callback.answer("Opened!")
        trade_state.pop(callback.from_user.id, None)
        # notification will be handled via worker, but also send rank
        from services.leaderboard import get_user_rank
        rank_info = await get_user_rank(session, comp.id, user.id)
        if rank_info:
            await callback.message.answer(f"Your rank: #{rank_info['rank']} ROI {rank_info['roi']}%")
    except Exception as e:
        await session.rollback()
        await callback.answer(str(e)[:200], show_alert=True)
        await callback.message.edit_text(f"❌ Failed: {e}")

@router.callback_query(F.data == "cancel_trade")
async def cb_cancel(callback: CallbackQuery):
    trade_state.pop(callback.from_user.id, None)
    await callback.message.edit_text("Cancelled")
    await callback.answer()

# Close position handler for /positions
@router.callback_query(F.data.startswith("close_pos:"))
async def cb_close_pos(callback: CallbackQuery, session):
    pos_id = int(callback.data.split(":")[1])
    result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer("No user")
        return
    acc_res = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    acc = acc_res.scalar_one_or_none()
    if not acc:
        await callback.answer("No account")
        return
    pos = await session.get(PaperPosition, pos_id)
    if not pos or pos.account_id != acc.id:
        await callback.answer("Not yours", show_alert=True)
        return
    if pos.status != "OPEN":
        await callback.answer("Already closed", show_alert=True)
        return
    try:
        await close_position(session, pos, acc, idempotency_key=f"tg_close:{callback.id}", reason="manual")
        # update participant
        from services.competition import get_or_create_default_competition, update_participant_equity
        comp = await get_or_create_default_competition(session)
        await update_participant_equity(session, user.id, comp.id)
        await session.commit()
        await callback.message.edit_text(f"✅ POSITION CLOSED\n\n{pos.symbol} {pos.side}\nEntry: {pos.entry_price}\nExit: {pos.current_price}\nPnL: {pos.realized_pnl}")
        await callback.answer("Closed")
    except Exception as e:
        await session.rollback()
        await callback.answer(str(e)[:200], show_alert=True)
