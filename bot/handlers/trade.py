from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from bot.views import back_keyboard, bingx_chart_url, fmt_money, get_display_snapshot
from config import settings
from db.models import User
from db.paper_models import Instrument, PaperPosition, PositionStatus, TradingAccount
from services.accounts import ensure_can_trade
from services.competition import get_or_create_default_competition, join_competition, update_participant_equity
from services.paper_adapter import close_position, open_position
from services.trading_account import get_or_create_trading_account

router = Router()
trade_state: dict[int, dict] = {}

LEVERAGES = ["1", "2", "5", "10", "20"]


def safe_trade_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "stale" in text or "unavailable" in text:
        return "⚠️ Рынок временно недоступен. Попробуйте ещё раз через несколько секунд."
    if "already" in text or ("open" in text and "position" in text):
        return "⚠️ Сделка уже обработана."
    if "margin" in text or "insufficient" in text:
        return "⚠️ Недостаточно доступной маржи."
    if "invalid" in text or "quantity" in text or "tp" in text:
        return "⚠️ Проверьте параметры сделки."
    if "competition" in text or "ended" in text:
        return "⚠️ Турнир уже завершён."
    return "⚠️ Сделка не выполнена."


def trade_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="1️⃣ Выбрать монету", callback_data="trade:coin")],
            [InlineKeyboardButton(text="2️⃣ Быстрое открытие", callback_data="trade:quick")],
        ]
    )


def leverage_keyboard(symbol: str, budget: str) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(text=f"{lev}x", callback_data=f"lev:{symbol}:{budget}:{lev}")
        for lev in LEVERAGES
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[row, [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_trade")]]
    )


def side_keyboard(symbol: str, budget: str, leverage: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔼 LONG", callback_data=f"side:{symbol}:{budget}:{leverage}:LONG"),
                InlineKeyboardButton(text="🔽 SHORT", callback_data=f"side:{symbol}:{budget}:{leverage}:SHORT"),
            ],
            [InlineKeyboardButton(text="◀️ К плечу", callback_data=f"re_lev:{symbol}:{budget}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_trade")],
        ]
    )


def tp_sl_keyboard(symbol: str, budget: str, leverage: str, side: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Установить TP/SL", callback_data=f"tpsl:set:{symbol}:{budget}:{leverage}:{side}")],
            [InlineKeyboardButton(text="⏭ Пропустить (без TP/SL)", callback_data=f"tpsl:skip:{symbol}:{budget}:{leverage}:{side}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_trade")],
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ ПОДТВЕРДИТЬ СДЕЛКУ", callback_data="trade:confirm")],
            [InlineKeyboardButton(text="❌ ОТМЕНА", callback_data="cancel_trade")],
        ]
    )


def normalize_ticker(raw: str) -> str | None:
    """SOL / sol-usdt / SOLUSDT -> SOLUSDT; None если не похоже на тикер."""
    value = raw.strip().upper().replace(" ", "").replace("/", "").replace("-", "")
    if not value or not value.isalnum():
        return None
    if not value.endswith("USDT"):
        value += "USDT"
    if len(value) > 20:
        return None
    return value


async def _validate_instrument(session, symbol: str) -> Instrument | None:
    inst = await session.get(Instrument, symbol)
    if inst is None or inst.status != "active":
        return None
    return inst


async def _account_line(session, user: User) -> str:
    account = (
        await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    ).scalar_one_or_none()
    if account is None:
        return "Баланс: —"
    return f"Доступно: {fmt_money(account.available_margin)}"


@router.message(Command("trade"))
@router.message(F.text == "🚀 Торговать")
async def cmd_trade(message: Message, session):
    await message.answer(
        "🚀 ТОРГОВЛЯ\n\n"
        "1️⃣ Выбрать монету — график пары на BingX\n"
        "2️⃣ Быстрое открытие — сделка за несколько шагов",
        reply_markup=trade_menu_keyboard(),
    )


@router.callback_query(F.data == "nav:trade")
async def nav_trade(callback: CallbackQuery, session):
    if callback.message:
        await cmd_trade(callback.message, session)
    await callback.answer()


@router.callback_query(F.data == "trade:coin")
async def cb_coin_select(callback: CallbackQuery):
    trade_state[callback.from_user.id] = {"awaiting": "ticker_chart"}
    await callback.message.edit_text(
        "1️⃣ ВЫБОР МОНЕТЫ\n\nВведите тикер (например: SOL или SOLUSDT).\n"
        "Я покажу ссылку на график пары на BingX.",
        reply_markup=back_keyboard("nav:trade"),
    )
    await callback.answer()


@router.callback_query(F.data == "trade:quick")
async def cb_quick_open(callback: CallbackQuery):
    trade_state[callback.from_user.id] = {"awaiting": "ticker_trade"}
    await callback.message.edit_text(
        "2️⃣ БЫСТРОЕ ОТКРЫТИЕ\n\nВведите тикер (например: SOL или SOLUSDT).",
        reply_markup=back_keyboard("nav:trade"),
    )
    await callback.answer()


@router.message(F.text)
async def handle_trade_text(message: Message, session):
    """Единая точка текстового ввода торгового мастера (тикер/бюджет/TP-SL)."""
    state = trade_state.get(message.from_user.id)
    if not state or "awaiting" not in state:
        return
    step = state["awaiting"]

    # --- Шаг: тикер ---
    if step in ("ticker_chart", "ticker_trade"):
        symbol = normalize_ticker(message.text or "")
        inst = await _validate_instrument(session, symbol) if symbol else None
        if symbol is None or inst is None:
            await message.answer("⚠️ Не нашёл такую пару на BingX. Введите тикер ещё раз (например: SOL).")
            return
        if step == "ticker_chart":
            trade_state.pop(message.from_user.id, None)
            snapshot = await get_display_snapshot(session, symbol)
            price_line = f"Текущая цена: {fmt_money(snapshot.last)}\n\n" if snapshot else ""
            await message.answer(
                f"📈 {inst.base_asset}/{inst.quote_asset} (BingX Perpetual)\n\n"
                f"{price_line}"
                f"График пары:\n{bingx_chart_url(symbol)}\n\n"
                f"Открыть сделку по {inst.base_asset} — кнопка ниже.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="2️⃣ Открыть сделку", callback_data=f"qsym:{symbol}")],
                        [InlineKeyboardButton(text="◀️ В меню торговли", callback_data="nav:trade")],
                    ]
                ),
            )
            return
        # quick open: ticker captured, next step budget
        trade_state[message.from_user.id] = {"symbol": symbol, "awaiting": "budget"}
        user = (await session.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one_or_none()
        if user is None:
            await message.answer("Сначала отправьте /start")
            return
        account_line = await _account_line(session, user)
        await message.answer(
            f"💰 БЮДЖЕТ СДЕЛКИ\n\n{symbol}\n{account_line}\n\n"
            "Введите сумму, которую готовы зарезервировать под сделку (маржу), в USD.\n"
            "Например: 100",
            reply_markup=back_keyboard("nav:trade"),
        )
        return

    # --- Шаг: бюджет ---
    if step == "budget":
        try:
            budget = Decimal((message.text or "").strip().replace(",", "."))
        except (InvalidOperation, ValueError):
            await message.answer("⚠️ Введите число, например: 100")
            return
        if not budget.is_finite() or budget <= 0:
            await message.answer("⚠️ Сумма должна быть положительным числом.")
            return
        symbol = state["symbol"]
        trade_state[message.from_user.id] = {
            "symbol": symbol,
            "budget": format(budget, "f"),
            "awaiting": "leverage",
        }
        await message.answer(
            f"⚖️ ПЛЕЧО\n\n{symbol} | Маржа: {fmt_money(budget)}\n\nВыберите плечо:",
            reply_markup=leverage_keyboard(symbol, format(budget, "f")),
        )
        return

    # --- Шаг: TP/SL ---
    if step == "tp_sl":
        text = (message.text or "").strip()
        tp = sl = None
        if text.lower() != "skip":
            parts = text.replace(",", " ").split()
            if len(parts) != 2:
                await message.answer("⚠️ Нужны ровно два числа через пробел: TP SL. Например: 180 160")
                return
            try:
                tp, sl = Decimal(parts[0]), Decimal(parts[1])
                if not tp.is_finite() or not sl.is_finite() or tp <= 0 or sl <= 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                await message.answer("⚠️ TP и SL должны быть положительными числами.")
                return
        st = dict(state)
        st.pop("awaiting", None)
        st["tp"], st["sl"] = tp, sl
        trade_state[message.from_user.id] = st
        await _show_confirmation(message, st, session)
        return


async def _show_confirmation(message: Message, state: dict, session):
    symbol = state["symbol"]
    budget = Decimal(state["budget"])
    leverage = Decimal(state["leverage"])
    side = state["side"]
    tp, sl = state.get("tp"), state.get("sl")
    snapshot = await get_display_snapshot(session, symbol)
    entry = None
    side_word = ""
    if snapshot:
        entry = snapshot.ask if side == "LONG" else snapshot.bid
        side_word = "ASK" if side == "LONG" else "BID"
    notional = (budget * leverage).quantize(Decimal("0.01"))
    state_line = (
        f"Цена входа ({side_word}): {fmt_money(entry)}\n" if entry else "⚠️ Цена временно недоступна — исполнение по серверной цене BingX.\n"
    )
    await message.answer(
        "⚡ ПОДТВЕРЖДЕНИЕ СДЕЛКИ\n\n"
        f"Пара: {symbol}\n"
        f"Направление: {'🔼 LONG' if side == 'LONG' else '🔽 SHORT'}\n"
        f"Маржа (бюджет): {fmt_money(budget)}\n"
        f"Плечо: {leverage:g}x | Объём: {fmt_money(notional)}\n\n"
        f"{state_line}"
        f"🎯 TP: {fmt_money(tp) if tp else 'нет'}\n"
        f"🛑 SL: {fmt_money(sl) if sl else 'нет'}\n\n"
        "Исполнение — по серверной цене BingX в момент подтверждения.",
        reply_markup=confirm_keyboard(),
    )


@router.callback_query(F.data.startswith("qsym:"))
async def cb_quick_symbol(callback: CallbackQuery, session):
    symbol = callback.data.split(":", 1)[1]
    inst = await _validate_instrument(session, symbol)
    if inst is None:
        await callback.answer("Пара недоступна", show_alert=True)
        return
    trade_state[callback.from_user.id] = {"symbol": symbol, "awaiting": "budget"}
    user = (await session.execute(select(User).where(User.telegram_id == callback.from_user.id))).scalar_one_or_none()
    if user is None:
        await callback.answer("Сначала отправьте /start", show_alert=True)
        return
    account_line = await _account_line(session, user)
    await callback.message.edit_text(
        f"2️⃣ БЫСТРОЕ ОТКРЫТИЕ\n\n{symbol}\n{account_line}\n\n"
        "Введите сумму маржи в USD. Например: 100",
        reply_markup=back_keyboard("nav:trade"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("re_lev:"))
async def cb_re_leverage(callback: CallbackQuery):
    _, symbol, budget = callback.data.split(":")
    trade_state[callback.from_user.id] = {
        "symbol": symbol,
        "budget": budget,
        "awaiting": "leverage",
    }
    await callback.message.edit_text(
        f"⚖️ ПЛЕЧО\n\n{symbol} | Маржа: {fmt_money(Decimal(budget))}\n\nВыберите плечо:",
        reply_markup=leverage_keyboard(symbol, budget),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lev:"))
async def cb_leverage(callback: CallbackQuery):
    _, symbol, budget, leverage = callback.data.split(":")
    if leverage not in LEVERAGES:
        await callback.answer("Некорректное плечо", show_alert=True)
        return
    trade_state[callback.from_user.id] = {
        "symbol": symbol,
        "budget": budget,
        "leverage": leverage,
        "awaiting": "side",
    }
    await callback.message.edit_text(
        f"🔼/🔽 НАПРАВЛЕНИЕ\n\n{symbol} | Маржа: {fmt_money(Decimal(budget))} | {leverage}x\n\nВыберите направление:",
        reply_markup=side_keyboard(symbol, budget, leverage),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("side:"))
async def cb_side(callback: CallbackQuery, session):
    _, symbol, budget, leverage, side = callback.data.split(":")
    if side not in ("LONG", "SHORT"):
        await callback.answer("Некорректное направление", show_alert=True)
        return
    trade_state[callback.from_user.id] = {
        "symbol": symbol,
        "budget": budget,
        "leverage": leverage,
        "side": side,
        "awaiting": "tp_sl",
    }
    await callback.message.edit_text(
        "🎯 TP/SL\n\n"
        "Можно установить уровни тейк-профита и стоп-лосса или пропустить этот шаг.",
        reply_markup=tp_sl_keyboard(symbol, budget, leverage, side),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("tpsl:"))
async def cb_tp_sl(callback: CallbackQuery, session):
    action, symbol, budget, leverage, side = callback.data.split(":")[1:]
    if action == "skip":
        st = {
            "symbol": symbol,
            "budget": budget,
            "leverage": leverage,
            "side": side,
            "tp": None,
            "sl": None,
        }
        trade_state[callback.from_user.id] = st
        await _show_confirmation(callback.message, st, session)
        await callback.answer()
        return
    trade_state[callback.from_user.id] = {
        "symbol": symbol,
        "budget": budget,
        "leverage": leverage,
        "side": side,
        "awaiting": "tp_sl",
    }
    await callback.message.edit_text(
        "🎯 ВВЕДИТЕ TP И SL\n\nДва числа через пробел (цены), например: 180 160\n"
        "LONG: TP выше входа, SL ниже.\nSHORT: TP ниже входа, SL выше.\n\n"
        "Чтобы вернуться без TP/SL — нажмите «Пропустить».",
        reply_markup=back_keyboard("nav:trade"),
    )
    await callback.answer()


@router.callback_query(F.data == "trade:confirm")
async def cb_confirm(callback: CallbackQuery, session):
    state = trade_state.get(callback.from_user.id, {})
    if not state.get("symbol"):
        await callback.answer("Сессия сделки устарела. Начните заново: /trade", show_alert=True)
        return
    if state.get("in_flight"):
        await callback.answer("Сделка уже обрабатывается…", show_alert=True)
        return

    user = (await session.execute(select(User).where(User.telegram_id == callback.from_user.id))).scalar_one_or_none()
    if not user:
        await callback.answer("Сначала отправьте /start", show_alert=True)
        return
    try:
        ensure_can_trade(user)
    except PermissionError as exc:
        await callback.answer(f"⚠️ {exc}", show_alert=True)
        return

    trade_state[callback.from_user.id]["in_flight"] = True
    try:
        account = await get_or_create_trading_account(session, user.id)
        competition = await get_or_create_default_competition(session)
        await join_competition(session, user.id, competition.id)
        position = await open_position(
            session,
            account,
            state["symbol"],
            state["side"],
            quantity=None,
            take_profit=state.get("tp"),
            stop_loss=state.get("sl"),
            idempotency_key=f"tg:{callback.id}",
            notional=Decimal(state["budget"]) * Decimal(state["leverage"]),
            competition_id=competition.id,
            requested_at=datetime.now(timezone.utc),
            leverage=Decimal(state["leverage"]),
        )
        await update_participant_equity(session, user.id, competition.id)
        await session.commit()
        trade_state.pop(callback.from_user.id, None)
        await callback.message.edit_text(
            "✅ ПОЗИЦИЯ ОТКРЫТА\n\n"
            f"{position.symbol} {'🔼 LONG' if position.side == 'LONG' else '🔽 SHORT'} x{position.leverage:g}\n"
            f"Вход: {fmt_money(position.entry_price)}\n"
            f"Маржа: {fmt_money(Decimal(state['budget']))} | Объём: {fmt_money(position.notional)}\n"
            f"🎯 TP: {fmt_money(position.take_profit) if position.take_profit else 'нет'}\n"
            f"🛑 SL: {fmt_money(position.stop_loss) if position.stop_loss else 'нет'}\n\n"
            "PnL обновляется в 📜 Мои сделки по живым ценам BingX.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📜 Мои сделки", callback_data="nav:transactions")],
                    [InlineKeyboardButton(text="🚀 Торговать", callback_data="nav:trade")],
                ]
            ),
        )
        await callback.answer("Позиция открыта")
    except Exception as exc:
        await session.rollback()
        message_text = safe_trade_error(exc)
        await callback.answer(message_text[:200], show_alert=True)
        if callback.message:
            await callback.message.edit_text(message_text, reply_markup=back_keyboard("nav:trade"))
    finally:
        st = trade_state.get(callback.from_user.id)
        if st:
            st.pop("in_flight", None)


@router.callback_query(F.data == "cancel_trade")
async def cb_cancel(callback: CallbackQuery):
    trade_state.pop(callback.from_user.id, None)
    if callback.message:
        await callback.message.edit_text("Сделка отменена.", reply_markup=back_keyboard("nav:trade"))
    await callback.answer()


@router.callback_query(F.data.startswith("close_preview:"))
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
        await callback.answer("Позиция уже закрыта", show_alert=True)
        return
    snapshot = await get_display_snapshot(session, position.symbol)
    current = snapshot.bid if position.side == "LONG" and snapshot else snapshot.ask if snapshot else None
    if current is not None:
        pnl = (
            (current - position.entry_price) * position.quantity
            if position.side == "LONG"
            else (position.entry_price - current) * position.quantity
        )
    else:
        pnl = None
    await callback.message.edit_text(
        "⚡ ЗАКРЫТИЕ ПОЗИЦИИ\n\n"
        f"{position.symbol} {'🔼 LONG' if position.side == 'LONG' else '🔽 SHORT'} x{position.leverage:g}\n"
        f"Вход: {fmt_money(position.entry_price)}\n"
        f"Текущая цена: {fmt_money(current) if current else '⚠️ рынок недоступен'}\n"
        f"Ожидаемый PnL: {fmt_money(pnl) if pnl is not None else '—'}\n\n"
        "LONG закроется по BID, SHORT — по ASK (серверная цена BingX).",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, закрыть", callback_data=f"close_confirm:{position.id}")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="nav:transactions")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("close_confirm:"))
async def cb_close_confirm(callback: CallbackQuery, session):
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
    try:
        closed, pnl = await close_position(
            session,
            position,
            account,
            idempotency_key=f"tg_close:{callback.id}",
            reason="manual",
        )
        if closed.competition_id:
            await update_participant_equity(session, user.id, closed.competition_id)
        await session.commit()
        await callback.message.edit_text(
            "✅ ПОЗИЦИЯ ЗАКРЫТА\n\n"
            f"{closed.symbol} {'🔼 LONG' if closed.side == 'LONG' else '🔽 SHORT'}\n"
            f"Выход: {fmt_money(closed.current_price)}\n"
            f"Реализованный PnL: {fmt_money(pnl)}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📜 Мои сделки", callback_data="nav:transactions")]]
            ),
        )
        await callback.answer("Позиция закрыта")
    except Exception as exc:
        await session.rollback()
        message_text = safe_trade_error(exc)
        await callback.answer(message_text[:200], show_alert=True)
        if callback.message:
            await callback.message.edit_text(message_text, reply_markup=back_keyboard("nav:transactions"))
