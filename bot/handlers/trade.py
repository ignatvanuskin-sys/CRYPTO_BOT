from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from bot.views import back_keyboard, fmt_money, get_display_snapshot
from config import settings
from db.models import User
from db.paper_models import PaperPosition, PositionStatus, TradingAccount
from services.bingx_market_data import MarketDataStale, MarketDataUnavailable
from services.competition import get_active_competition, join_competition, update_participant_equity
from services.leaderboard import get_user_rank
from services.metrics import increment
from services.paper_adapter import PaperError, close_position, open_position

router = Router()
trade_state: dict[int, dict] = {}


def safe_trade_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "stale" in text or "unavailable" in text:
        return "⚠️ Рынок временно недоступен. Попробуй ещё раз через несколько секунд."
    if "already" in text or ("open" in text and "position" in text):
        return "⚠️ Сделка уже обработана."
    if "margin" in text or "insufficient" in text:
        return "⚠️ Недостаточно доступной маржи."
    if "invalid" in text or "quantity" in text or "tp" in text:
        return "⚠️ Проверь параметры сделки."
    if "competition" in text or "ended" in text:
        return "⚠️ Турнир уже завершён."
    return "⚠️ Сделка не выполнена."


def asset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="₿ BTC", callback_data="asset:BTCUSDT"),
                InlineKeyboardButton(text="Ξ ETH", callback_data="asset:ETHUSDT"),
                InlineKeyboardButton(text="◎ SOL", callback_data="asset:SOLUSDT"),
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="nav:home")],
        ]
    )


def side_keyboard(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔼 ЛОНГ", callback_data=f"side:{symbol}:LONG"),
                InlineKeyboardButton(text="🔽 ШОРТ", callback_data=f"side:{symbol}:SHORT"),
            ],
            [InlineKeyboardButton(text="◀️ К активам", callback_data="nav:trade")],
        ]
    )


def size_keyboard(symbol: str, side: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="$100", callback_data=f"size:{symbol}:{side}:100"),
                InlineKeyboardButton(text="$500", callback_data=f"size:{symbol}:{side}:500"),
                InlineKeyboardButton(text="$1,000", callback_data=f"size:{symbol}:{side}:1000"),
            ],
            [InlineKeyboardButton(text="$2,500", callback_data=f"size:{symbol}:{side}:2500")],
            [InlineKeyboardButton(text="✏️ Свой размер", callback_data=f"size_custom:{symbol}:{side}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"asset:{symbol}")],
        ]
    )


def tp_sl_keyboard(symbol: str, side: str, notional: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Установить TP/SL", callback_data=f"tp_sl:{symbol}:{side}:{notional}")],
            [InlineKeyboardButton(text="Без TP/SL", callback_data=f"confirm:{symbol}:{side}:{notional}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_trade")],
        ]
    )


def confirm_keyboard(symbol: str, side: str, notional: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ ОТКРЫТЬ", callback_data=f"confirm:{symbol}:{side}:{notional}")],
            [InlineKeyboardButton(text="❌ ОТМЕНА", callback_data="cancel_trade")],
        ]
    )


async def _price_line(session, symbol: str, side: str) -> tuple[str, Decimal | None]:
    snapshot = await get_display_snapshot(session, symbol)
    if not snapshot or not snapshot.bid or not snapshot.ask:
        return "⚠️ Актуальная цена загружается…", None
    price = snapshot.ask if side == "LONG" else snapshot.bid
    return f"Цена: {fmt_money(price)} {'ASK' if side == 'LONG' else 'BID'}", price


@router.message(Command("trade"))
@router.message(F.text == "🚀 Торговать")
async def cmd_trade(message: Message):
    increment("trade_flow_started")
    await message.answer("🚀 ТОРГОВАТЬ\n\nВыбери актив:", reply_markup=asset_keyboard())


@router.callback_query(F.data == "nav:trade")
async def nav_trade(callback: CallbackQuery):
    await cmd_trade(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("asset:"))
async def cb_asset(callback: CallbackQuery, session):
    symbol = callback.data.split(":", 1)[1]
    if symbol not in {"BTCUSDT", "ETHUSDT", "SOLUSDT"}:
        await callback.answer("Актив недоступен", show_alert=True)
        return
    await callback.message.edit_text(
        f"{symbol.replace('USDT', ' / USDT')}\n\nВыбери направление:",
        reply_markup=side_keyboard(symbol),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("side:"))
async def cb_side(callback: CallbackQuery, session):
    _, symbol, side = callback.data.split(":")
    if symbol not in {"BTCUSDT", "ETHUSDT", "SOLUSDT"} or side not in {"LONG", "SHORT"}:
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    trade_state[callback.from_user.id] = {"symbol": symbol, "side": side}
    price_line, _ = await _price_line(session, symbol, side)
    await callback.message.edit_text(
        f"{symbol.replace('USDT', ' / USDT')}\n\n{price_line}\n\n"
        "💰 Баланс и размер позиции:\nвыбери сумму:",
        reply_markup=size_keyboard(symbol, side),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("size:"))
async def cb_size(callback: CallbackQuery, session):
    _, symbol, side, notional = callback.data.split(":")
    try:
        amount = Decimal(notional)
        if not amount.is_finite() or amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await callback.answer("Некорректный размер", show_alert=True)
        return
    trade_state[callback.from_user.id] = {"symbol": symbol, "side": side, "notional": notional}
    price_line, price = await _price_line(session, symbol, side)
    await callback.message.edit_text(
        "⚡ ПОДТВЕРЖДЕНИЕ ПАРАМЕТРОВ\n\n"
        f"{symbol.replace('USDT', ' / USDT')}\n"
        f"{'🔼 LONG' if side == 'LONG' else '🔽 SHORT'}\n\n"
        f"Размер: {fmt_money(amount)}\n{price_line}\n\n"
        "TP/SL можно добавить отдельно.",
        reply_markup=tp_sl_keyboard(symbol, side, notional),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("size_custom:"))
async def cb_size_custom(callback: CallbackQuery):
    _, symbol, side = callback.data.split(":")
    trade_state[callback.from_user.id] = {"symbol": symbol, "side": side, "awaiting": "notional"}
    await callback.message.edit_text(
        f"Введи размер позиции в USD для {symbol.replace('USDT', ' / USDT')}\n\nНапример: 500",
        reply_markup=back_keyboard("nav:trade"),
    )
    await callback.answer()


@router.message(F.text.regexp(r"^\d+(\.\d+)?$"))
async def handle_custom_size(message: Message, session):
    state = trade_state.get(message.from_user.id)
    if not state or state.get("awaiting") != "notional":
        return
    try:
        amount = Decimal(message.text.strip())
        if not amount.is_finite() or amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await message.answer("⚠️ Введи положительную сумму, например 500.")
        return
    symbol, side = state["symbol"], state["side"]
    notional = format(amount, "f")
    trade_state[message.from_user.id] = {"symbol": symbol, "side": side, "notional": notional}
    price_line, _ = await _price_line(session, symbol, side)
    await message.answer(
        f"Размер: {fmt_money(amount)}\n{price_line}\n\nДобавить TP/SL?",
        reply_markup=tp_sl_keyboard(symbol, side, notional),
    )


@router.callback_query(F.data.startswith("tp_sl:"))
async def cb_tp_sl(callback: CallbackQuery):
    _, symbol, side, notional = callback.data.split(":")
    trade_state[callback.from_user.id] = {
        "symbol": symbol,
        "side": side,
        "notional": notional,
        "awaiting": "tp_sl",
    }
    await callback.message.edit_text(
        f"Введи TP и SL двумя числами через пробел.\n\nНапример: 51000 49500\n\nДля сделки без уровней напиши skip.",
        reply_markup=back_keyboard("nav:trade"),
    )
    await callback.answer()


@router.message(F.text.regexp(r"^(skip|\d+.*\d+.*)$"))
async def handle_tp_sl(message: Message, session):
    state = trade_state.get(message.from_user.id)
    if not state or state.get("awaiting") != "tp_sl":
        return
    text = message.text.strip()
    tp = sl = None
    if text.lower() != "skip":
        parts = text.split()
        if len(parts) != 2:
            await message.answer("⚠️ Нужны ровно два числа: TP SL.")
            return
        try:
            tp, sl = Decimal(parts[0]), Decimal(parts[1])
            if not tp.is_finite() or not sl.is_finite() or tp <= 0 or sl <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            await message.answer("⚠️ TP и SL должны быть положительными числами.")
            return
    symbol, side, notional = state["symbol"], state["side"], state["notional"]
    trade_state[message.from_user.id] = {"symbol": symbol, "side": side, "notional": notional, "tp": tp, "sl": sl}
    price_line, _ = await _price_line(session, symbol, side)
    await message.answer(
        "⚡ ПОДТВЕРЖДЕНИЕ\n\n"
        f"{symbol.replace('USDT', ' / USDT')} {'🔼 LONG' if side == 'LONG' else '🔽 SHORT'}\n"
        f"Размер: {fmt_money(Decimal(notional))}\n{price_line}\n"
        f"🎯 TP: {fmt_money(tp)}\n🛑 SL: {fmt_money(sl)}\n\n"
        "Цена исполнения будет взята сервером с BingX.",
        reply_markup=confirm_keyboard(symbol, side, notional),
    )


@router.callback_query(F.data.startswith("confirm:"))
async def cb_confirm(callback: CallbackQuery, session):
    _, symbol, side, notional = callback.data.split(":")
    state = trade_state.get(callback.from_user.id, {})
    if state.get("symbol") != symbol or state.get("side") != side or state.get("notional") != notional:
        await callback.answer("⚠️ Сессия сделки устарела. Начни заново через /trade.", show_alert=True)
        return
    try:
        amount = Decimal(notional)
        if not amount.is_finite() or amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await callback.answer("Некорректный размер", show_alert=True)
        return

    user = (await session.execute(select(User).where(User.telegram_id == callback.from_user.id))).scalar_one_or_none()
    if not user:
        await callback.answer("Сначала отправь /start", show_alert=True)
        return
    account = (await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    if not account:
        await callback.answer("Сначала отправь /start", show_alert=True)
        return
    competition = await get_active_competition(session)
    if competition is None:
        await callback.answer("⚠️ Турнир завершён или ещё не начался.", show_alert=True)
        await callback.message.edit_text(
            "⚠️ Турнир завершён или ещё не начался.\nОтправь /start, чтобы присоединиться к следующему.",
            reply_markup=back_keyboard("nav:home"),
        )
        return
    tp, sl = state.get("tp"), state.get("sl")
    try:
        await join_competition(session, user.id, competition.id)
        position = await open_position(
            session,
            account,
            symbol,
            side,
            quantity=None,
            take_profit=tp,
            stop_loss=sl,
            idempotency_key=f"tg:{callback.id}",
            notional=amount,
            competition_id=competition.id,
            requested_at=datetime.now(timezone.utc),
        )
        await update_participant_equity(session, user.id, competition.id)
        await session.commit()
        trade_state.pop(callback.from_user.id, None)
        increment("trade_flow_completed")
        rank = await get_user_rank(session, competition.id, user.id)
        rank_line = f"\n🏆 Текущий ранг: #{rank['rank']}" if rank else ""
        await callback.message.edit_text(
            "✅ ПОЗИЦИЯ ОТКРЫТА\n\n"
            f"{symbol.replace('USDT', ' / USDT')} {'🔼 LONG' if side == 'LONG' else '🔽 SHORT'}\n"
            f"Вход: {fmt_money(position.entry_price)}\n"
            f"Размер: {fmt_money(amount)}\n"
            f"🎯 TP: {fmt_money(tp)}\n🛑 SL: {fmt_money(sl)}\n\n"
            f"Цена и PnL считаются сервером.{rank_line}",
            reply_markup=back_keyboard("nav:home"),
        )
        await callback.answer("Позиция открыта")
    except Exception as exc:
        await session.rollback()
        message = safe_trade_error(exc)
        await callback.answer(message[:200], show_alert=True)
        await callback.message.edit_text(message, reply_markup=back_keyboard("nav:trade"))


@router.callback_query(F.data == "cancel_trade")
async def cb_cancel(callback: CallbackQuery):
    trade_state.pop(callback.from_user.id, None)
    await callback.message.edit_text("Сделка отменена.", reply_markup=back_keyboard("nav:home"))
    await callback.answer()


@router.callback_query(F.data.startswith("close_preview:"))
@router.callback_query(F.data.startswith("close_pos:"))
async def cb_close_preview(callback: CallbackQuery, session):
    try:
        pos_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    user = (await session.execute(select(User).where(User.telegram_id == callback.from_user.id))).scalar_one_or_none()
    account = (
        await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    ).scalar_one_or_none() if user else None
    position = await session.get(PaperPosition, pos_id) if account else None
    if not position or position.account_id != account.id:
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    if position.status != PositionStatus.OPEN.value:
        await callback.answer("⚠️ Позиция уже закрыта", show_alert=True)
        return
    snapshot = await get_display_snapshot(session, position.symbol)
    current = snapshot.bid if position.side == "LONG" and snapshot else snapshot.ask if snapshot else None
    pnl = ((position.entry_price - current) * position.quantity if position.side == "SHORT" else (current - position.entry_price) * position.quantity) if current else None
    await callback.message.edit_text(
        "⚡ ЗАКРЫТИЕ\n\n"
        f"{position.symbol} {position.side}\n"
        f"Текущая цена: {fmt_money(current) if current else '⚠️ рынок no_data'}\n"
        f"Ожидаемый PnL: {fmt_money(pnl) if pnl is not None else '—'}\n\n"
        "Цена исполнения будет получена сервером с BingX.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ ДА, ЗАКРЫТЬ", callback_data=f"close_confirm:{position.id}")],
                [InlineKeyboardButton(text="❌ ОТМЕНА", callback_data="nav:positions")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("close_confirm:"))
async def cb_close_confirm(callback: CallbackQuery, session):
    pos_id = int(callback.data.split(":", 1)[1])
    user = (await session.execute(select(User).where(User.telegram_id == callback.from_user.id))).scalar_one_or_none()
    account = (
        await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    ).scalar_one_or_none() if user else None
    position = await session.get(PaperPosition, pos_id) if account else None
    if not position or position.account_id != account.id:
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    try:
        closed, pnl = await close_position(
            session,
            position,
            account,
            idempotency_key=f"tg_close:{callback.id}",
            reason="manual",
        )
        competition = await get_active_competition(session)
        if competition is not None:
            await update_participant_equity(session, user.id, competition.id)
        await session.commit()
        await callback.message.edit_text(
            "✅ ПОЗИЦИЯ ЗАКРЫТА\n\n"
            f"{closed.symbol} {closed.side}\n"
            f"Выход: {fmt_money(closed.current_price)}\n"
            f"Реализованный PnL: {fmt_money(pnl)}",
            reply_markup=back_keyboard("nav:positions"),
        )
        await callback.answer("Позиция закрыта")
    except Exception as exc:
        await session.rollback()
        message = safe_trade_error(exc)
        await callback.answer(message[:200], show_alert=True)
        await callback.message.edit_text(message, reply_markup=back_keyboard("nav:positions"))
