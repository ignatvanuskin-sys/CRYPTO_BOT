from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from bot.emojis import (
    BOOKMARK_ID,
    BOOM_ID,
    BULB_ID,
    CHART_ID,
    CHART_UP_ID,
    CHECK_ID,
    CROSS_ID,
    DIAMOND_ID,
    FREE_ID,
    GEAR_ID,
    GREEN_ID,
    LONG_EMOJI_ID,
    MONEY_ID,
    PIN_ID,
    RED_ID,
    SHORT_EMOJI_ID,
    SIREN_ID,
    STAR_ID,
    WARNING_ID,
    tg_emoji,
    TG_LONG,
    TG_SHORT,
)
from bot.views import back_keyboard, bingx_chart_url, fmt_money, fmt_price, format_side, get_display_snapshot
from config import settings
from db.models import User
from db.paper_models import Instrument, PaperPosition, PositionStatus, TradingAccount
from services.accounts import ensure_can_trade
from services.competition import get_or_create_default_competition, join_competition, update_participant_equity
from services.paper_adapter import close_position, open_position
from services.trading_account import get_or_create_trading_account

router = Router()
trade_state: dict[int, dict] = {}

LEVERAGES = ["1", "2", "5", "10", "20", "50", "100", "150", "300"]

TG_WARNING = tg_emoji(WARNING_ID, "⚠️")
TG_CHECK = tg_emoji(CHECK_ID, "✔️")
TG_CROSS = tg_emoji(CROSS_ID, "❌")
TG_MONEY = tg_emoji(MONEY_ID, "💵")
TG_CHART = tg_emoji(CHART_ID, "📊")
TG_CHART_UP = tg_emoji(CHART_UP_ID, "📈")
TG_GEAR = tg_emoji(GEAR_ID, "⚙️")
TG_STAR = tg_emoji(STAR_ID, "⭐️")
TG_SIREN = tg_emoji(SIREN_ID, "🚨")
TG_GREEN = tg_emoji(GREEN_ID, "🟢")
TG_RED = tg_emoji(RED_ID, "🔴")


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def safe_trade_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "stale" in text or "unavailable" in text:
        return f"{TG_WARNING} Рынок временно недоступен. Попробуйте ещё раз через несколько секунд."
    if "already" in text or ("open" in text and "position" in text):
        return f"{TG_WARNING} Сделка уже обработана."
    if "margin" in text or "insufficient" in text:
        return f"{TG_WARNING} Недостаточно доступной маржи."
    if "invalid" in text or "quantity" in text or "tp" in text:
        return f"{TG_WARNING} Проверьте параметры сделки."
    if "competition" in text or "ended" in text:
        return f"{TG_WARNING} Турнир уже завершён."
    return f"{TG_WARNING} Сделка не выполнена."


def trade_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="1. Выбрать монету", callback_data="trade:coin", icon_custom_emoji_id=DIAMOND_ID)],
            [InlineKeyboardButton(text="2. Быстрое открытие", callback_data="trade:quick", icon_custom_emoji_id=BOOM_ID)],
        ]
    )


def leverage_keyboard(symbol: str, budget: str, max_leverage: int | None = None) -> InlineKeyboardMarkup:
    # Filter by max_leverage for this symbol (BingX per-coin tier)
    allowed = LEVERAGES
    if max_leverage is not None:
        try:
            ml = int(max_leverage)
            allowed = [lv for lv in LEVERAGES if int(lv) <= ml]
            if not allowed:
                allowed = ["1"]
        except Exception:
            pass
    row1 = [
        InlineKeyboardButton(text=f"{lev}x", callback_data=f"lev:{symbol}:{budget}:{lev}", icon_custom_emoji_id=GEAR_ID)
        for lev in allowed[:5]
    ]
    row2 = [
        InlineKeyboardButton(text=f"{lev}x", callback_data=f"lev:{symbol}:{budget}:{lev}", icon_custom_emoji_id=GEAR_ID)
        for lev in allowed[5:]
    ]
    rows = []
    if row1:
        rows.append(row1)
    if row2:
        rows.append(row2)
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel_trade", icon_custom_emoji_id=CROSS_ID)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def side_keyboard(symbol: str, budget: str, leverage: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="LONG", callback_data=f"side:{symbol}:{budget}:{leverage}:LONG", icon_custom_emoji_id=LONG_EMOJI_ID),
                InlineKeyboardButton(text="SHORT", callback_data=f"side:{symbol}:{budget}:{leverage}:SHORT", icon_custom_emoji_id=SHORT_EMOJI_ID),
            ],
            [InlineKeyboardButton(text="К плечу", callback_data=f"re_lev:{symbol}:{budget}", icon_custom_emoji_id=GEAR_ID)],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel_trade", icon_custom_emoji_id=CROSS_ID)],
        ]
    )


def tp_sl_keyboard(symbol: str, budget: str, leverage: str, side: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Установить TP/SL", callback_data=f"tpsl:set:{symbol}:{budget}:{leverage}:{side}", icon_custom_emoji_id=STAR_ID)],
            [InlineKeyboardButton(text="Пропустить", callback_data=f"tpsl:skip:{symbol}:{budget}:{leverage}:{side}", icon_custom_emoji_id=FREE_ID)],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel_trade", icon_custom_emoji_id=CROSS_ID)],
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подтвердить сделку", callback_data="trade:confirm", icon_custom_emoji_id=CHECK_ID)],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel_trade", icon_custom_emoji_id=CROSS_ID)],
        ]
    )


def normalize_ticker(raw: str) -> str | None:
    """SOL / sol-usdt / SOLUSDT -> SOLUSDT; None если не похоже на тикер."""
    value = raw.strip().upper().replace(" ", "").replace("/", "").replace("-", "")
    if not value or not value.isalnum():
        return None
    if not value.endswith("USDT"):
        value += "USDT"
    if len(value) > 40:
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
@router.message(F.text == "Торговать")
async def cmd_trade(message: Message, session):
    await message.answer(
        f"{TG_CHART_UP} <b>ТОРГОВЛЯ</b>\n\n"
        f"{tg_emoji(DIAMOND_ID, '💎')} Выбрать монету — график пары на BingX\n"
        f"{tg_emoji(BOOM_ID, '💥')} Быстрое открытие — сделка за несколько шагов",
        parse_mode=ParseMode.HTML,
        reply_markup=trade_menu_keyboard(),
    )


@router.callback_query(F.data == "nav:trade")
async def nav_trade(callback: CallbackQuery, session):
    if callback.from_user is not None:
        trade_state.pop(callback.from_user.id, None)
    if callback.message is None:
        await callback.answer()
        return
    await cmd_trade(callback.message, session)
    await callback.answer()


@router.callback_query(F.data == "trade:coin")
async def cb_coin_select(callback: CallbackQuery):
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    trade_state[callback.from_user.id] = {"awaiting": "ticker_chart"}
    await callback.message.edit_text(
        f"{tg_emoji(DIAMOND_ID, '💎')} <b>ВЫБОР МОНЕТЫ</b>\n\nВведите тикер (например: SOL или SOLUSDT).\n"
        f"Я покажу ссылку на график пары на BingX.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard("nav:trade"),
    )
    await callback.answer()


@router.callback_query(F.data == "trade:quick")
async def cb_quick_open(callback: CallbackQuery):
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    trade_state[callback.from_user.id] = {"awaiting": "ticker_trade"}
    await callback.message.edit_text(
        f"{tg_emoji(BOOM_ID, '💥')} <b>БЫСТРОЕ ОТКРЫТИЕ</b>\n\nВведите тикер (например: SOL или SOLUSDT).",
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard("nav:trade"),
    )
    await callback.answer()


@router.message(F.text)
async def handle_trade_text(message: Message, session):
    """Единая точка текстового ввода торгового мастера (тикер/бюджет/TP-SL)."""
    if message.from_user is None or message.text is None:
        return
    # Не перехватываем команды и навигацию — даём другим хендлерам шанс
    if message.text.startswith("/") or message.text in ("Личный кабинет", "Сделки", "Торговать"):
        trade_state.pop(message.from_user.id, None)
        return
    state = trade_state.get(message.from_user.id)
    if not state or "awaiting" not in state:
        return
    step = state["awaiting"]

    # --- Шаг: тикер ---
    if step in ("ticker_chart", "ticker_trade"):
        symbol = normalize_ticker(message.text or "")
        inst = await _validate_instrument(session, symbol) if symbol else None
        if symbol is None or inst is None:
            await message.answer(f"{TG_WARNING} Не нашёл такую пару на BingX. Введите тикер ещё раз (например: SOL).", parse_mode=ParseMode.HTML)
            return
        if step == "ticker_chart":
            trade_state.pop(message.from_user.id, None)
            snapshot = await get_display_snapshot(session, symbol)
            price_line = f"Текущая цена: {fmt_price(snapshot.last)}\n\n" if snapshot else ""
            await message.answer(
                f"{TG_CHART} {inst.base_asset}/{inst.quote_asset} (BingX Perpetual)\n\n"
                f"{price_line}"
                f"График пары:\n{bingx_chart_url(symbol)}\n\n"
                f"Открыть сделку по {inst.base_asset} — кнопка ниже.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Открыть сделку", callback_data=f"qsym:{symbol}", icon_custom_emoji_id=BOOM_ID)],
                        [InlineKeyboardButton(text="В меню торговли", callback_data="nav:trade", icon_custom_emoji_id=PIN_ID)],
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
            f"{TG_MONEY} <b>БЮДЖЕТ СДЕЛКИ</b>\n\n{symbol}\n{account_line}\n\n"
            "Введите сумму, которую готовы зарезервировать под сделку (маржу), в USD.\n"
            "Например: 100",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard("nav:trade"),
        )
        return

    # --- Шаг: бюджет ---
    if step == "budget":
        raw = (message.text or "").strip()
        if len(raw) > 20:
            await message.answer(f"{TG_WARNING} Слишком длинный ввод.", parse_mode=ParseMode.HTML)
            return
        try:
            budget = Decimal(raw.replace(",", "."))
        except (InvalidOperation, ValueError):
            await message.answer(f"{TG_WARNING} Введите число, например: 100", parse_mode=ParseMode.HTML)
            return
        if not budget.is_finite() or budget <= 0:
            await message.answer(f"{TG_WARNING} Сумма должна быть положительным числом.", parse_mode=ParseMode.HTML)
            return
        if budget > Decimal("1000000"):
            await message.answer(f"{TG_WARNING} Слишком большая сумма.", parse_mode=ParseMode.HTML)
            return
        symbol = state["symbol"]
        trade_state[message.from_user.id] = {
            "symbol": symbol,
            "budget": format(budget, "f"),
            "awaiting": "leverage",
        }
        # Filter leverage by instrument max
        max_lev = None
        inst = await session.get(Instrument, symbol)
        if inst and inst.max_leverage:
            max_lev = inst.max_leverage
        await message.answer(
            f"{TG_GEAR} <b>ПЛЕЧО</b>\n\n{symbol} | Маржа: {fmt_money(budget)}\n\nВыберите плечо:",
            parse_mode=ParseMode.HTML,
            reply_markup=leverage_keyboard(symbol, format(budget, "f"), max_lev),
        )
        return

    # --- Шаг: TP/SL ---
    if step == "tp_sl":
        text = (message.text or "").strip()
        tp = sl = None
        if text.lower() != "skip":
            parts = text.replace(",", " ").split()
            if len(parts) != 2:
                await message.answer(f"{TG_WARNING} Нужны ровно два числа через пробел: TP SL. Например: 180 160", parse_mode=ParseMode.HTML)
                return
            try:
                tp, sl = Decimal(parts[0]), Decimal(parts[1])
                if not tp.is_finite() or not sl.is_finite() or tp <= 0 or sl <= 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                await message.answer(f"{TG_WARNING} TP и SL должны быть положительными числами.", parse_mode=ParseMode.HTML)
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
    side_tag = TG_LONG if side == "LONG" else TG_SHORT
    state_line = (
        f"Цена входа ({side_word}): {fmt_price(entry)}\n" if entry else f"{TG_WARNING} Цена временно недоступна — исполнение по серверной цене BingX.\n"
    )
    await message.answer(
        f"{TG_SIREN} <b>ПОДТВЕРЖДЕНИЕ СДЕЛКИ</b>\n\n"
        f"Пара: {symbol}\n"
        f"Направление: {side_tag} {side}\n"
        f"Маржа (бюджет): {fmt_money(budget)}\n"
        f"Плечо: {leverage:g}x | Объём: {fmt_money(notional)}\n\n"
        f"{state_line}"
        f"{TG_STAR} TP: {fmt_price(tp) if tp else 'нет'}\n"
        f"{tg_emoji(RED_ID, '🔴')} SL: {fmt_price(sl) if sl else 'нет'}\n\n"
        "Исполнение — по серверной цене BingX в момент подтверждения.",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
    )


@router.callback_query(F.data.startswith("qsym:"))
async def cb_quick_symbol(callback: CallbackQuery, session):
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
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
        f"{TG_MONEY} <b>БЫСТРОЕ ОТКРЫТИЕ</b>\n\n{symbol}\n{account_line}\n\n"
        "Введите сумму маржи в USD. Например: 100",
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard("nav:trade"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("re_lev:"))
async def cb_re_leverage(callback: CallbackQuery, session):
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    try:
        _, symbol, budget = callback.data.split(":")
    except ValueError:
        await callback.answer("Некорректные данные", show_alert=True)
        return
    trade_state[callback.from_user.id] = {
        "symbol": symbol,
        "budget": budget,
        "awaiting": "leverage",
    }
    max_lev = None
    inst = await session.get(Instrument, symbol)
    if inst and inst.max_leverage:
        max_lev = inst.max_leverage
    await callback.message.edit_text(
        f"{TG_GEAR} <b>ПЛЕЧО</b>\n\n{symbol} | Маржа: {fmt_money(Decimal(budget))}\n\nВыберите плечо:",
        parse_mode=ParseMode.HTML,
        reply_markup=leverage_keyboard(symbol, budget, max_lev),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lev:"))
async def cb_leverage(callback: CallbackQuery):
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    try:
        _, symbol, budget, leverage = callback.data.split(":")
    except ValueError:
        await callback.answer("Некорректные данные", show_alert=True)
        return
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
        f"{TG_LONG} {TG_SHORT} <b>НАПРАВЛЕНИЕ</b>\n\n{symbol} | Маржа: {fmt_money(Decimal(budget))} | {leverage}x\n\nВыберите направление:",
        parse_mode=ParseMode.HTML,
        reply_markup=side_keyboard(symbol, budget, leverage),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("side:"))
async def cb_side(callback: CallbackQuery, session):
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    try:
        _, symbol, budget, leverage, side = callback.data.split(":")
    except ValueError:
        await callback.answer("Некорректные данные", show_alert=True)
        return
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
        f"{TG_STAR} <b>TP/SL</b>\n\n"
        "Можно установить уровни тейк-профита и стоп-лосса или пропустить этот шаг.",
        parse_mode=ParseMode.HTML,
        reply_markup=tp_sl_keyboard(symbol, budget, leverage, side),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("tpsl:"))
async def cb_tp_sl(callback: CallbackQuery, session):
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    try:
        action, symbol, budget, leverage, side = callback.data.split(":")[1:]
    except ValueError:
        await callback.answer("Некорректные данные", show_alert=True)
        return
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
        f"{TG_STAR} <b>ВВЕДИТЕ TP И SL</b>\n\nДва числа через пробел (цены), например: 180 160\n"
        "LONG: TP выше входа, SL ниже.\nSHORT: TP ниже входа, SL выше.\n\n"
        "Чтобы вернуться без TP/SL — нажмите «Пропустить».",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data=f"tpsl:back:{symbol}:{budget}:{leverage}:{side}", icon_custom_emoji_id=PIN_ID)]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("tpsl:back:"))
async def cb_tp_sl_back(callback: CallbackQuery, session):
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    try:
        _, _, symbol, budget, leverage, side = callback.data.split(":")
    except ValueError:
        await callback.answer("Некорректные данные", show_alert=True)
        return
    trade_state[callback.from_user.id] = {
        "symbol": symbol,
        "budget": budget,
        "leverage": leverage,
        "side": side,
        "awaiting": "tp_sl",
    }
    await callback.message.edit_text(
        f"{TG_STAR} <b>TP/SL</b>\n\n"
        "Можно установить уровни тейк-профита и стоп-лосса или пропустить этот шаг.",
        parse_mode=ParseMode.HTML,
        reply_markup=tp_sl_keyboard(symbol, budget, leverage, side),
    )
    await callback.answer()


@router.callback_query(F.data == "trade:confirm")
async def cb_confirm(callback: CallbackQuery, session):
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
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
        await callback.answer(_strip_tags(f"{TG_WARNING} {exc}")[:200], show_alert=True)
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
        side_tag = TG_LONG if position.side == "LONG" else TG_SHORT
        await callback.message.edit_text(
            f"{TG_CHECK} <b>ПОЗИЦИЯ ОТКРЫТА</b>\n\n"
            f"{position.symbol} {side_tag} {format_side(position.side)} x{position.leverage:g}\n"
            f"Вход: {fmt_price(position.entry_price)}\n"
            f"Маржа: {fmt_money(Decimal(state['budget']))} | Объём: {fmt_money(position.notional)}\n"
            f"{TG_STAR} TP: {fmt_price(position.take_profit) if position.take_profit else 'нет'}\n"
            f"{tg_emoji(RED_ID, '🔴')} SL: {fmt_price(position.stop_loss) if position.stop_loss else 'нет'}\n\n"
            f"PnL обновляется в {TG_CHART} Мои сделки по живым ценам BingX.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Мои сделки", callback_data="nav:transactions", icon_custom_emoji_id=CHART_ID)],
                    [InlineKeyboardButton(text="Торговать", callback_data="nav:trade", icon_custom_emoji_id=CHART_UP_ID)],
                ]
            ),
        )
        await callback.answer("Позиция открыта")
    except Exception as exc:
        await session.rollback()
        html_text = safe_trade_error(exc)
        plain = _strip_tags(html_text)
        await callback.answer(plain[:200], show_alert=True)
        if callback.message:
            await callback.message.edit_text(html_text, parse_mode=ParseMode.HTML, reply_markup=back_keyboard("nav:trade"))
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
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
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
    side_tag = TG_LONG if position.side == "LONG" else TG_SHORT
    await callback.message.edit_text(
        f"{TG_SIREN} <b>ЗАКРЫТИЕ ПОЗИЦИИ</b>\n\n"
        f"{position.symbol} {side_tag} {format_side(position.side)} x{position.leverage:g}\n"
        f"Вход: {fmt_price(position.entry_price)}\n"
        f"Текущая цена: {fmt_price(current) if current else f'{TG_WARNING} рынок недоступен'}\n"
        f"Ожидаемый PnL: {fmt_money(pnl) if pnl is not None else '—'}\n\n"
        "LONG закроется по BID, SHORT — по ASK (серверная цена BingX).",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Да, закрыть", callback_data=f"close_confirm:{position.id}", icon_custom_emoji_id=CHECK_ID)],
                [InlineKeyboardButton(text="Отмена", callback_data="nav:transactions", icon_custom_emoji_id=CROSS_ID)],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("close_confirm:"))
async def cb_close_confirm(callback: CallbackQuery, session):
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
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
        side_tag = TG_LONG if closed.side == "LONG" else TG_SHORT
        await callback.message.edit_text(
            f"{TG_CHECK} <b>ПОЗИЦИЯ ЗАКРЫТА</b>\n\n"
            f"{closed.symbol} {side_tag} {format_side(closed.side)}\n"
            f"Выход: {fmt_price(closed.current_price)}\n"
            f"Реализованный PnL: {fmt_money(pnl)}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Мои сделки", callback_data="nav:transactions", icon_custom_emoji_id=CHART_ID)]]
            ),
        )
        await callback.answer("Позиция закрыта")
    except Exception as exc:
        await session.rollback()
        html_text = safe_trade_error(exc)
        plain = _strip_tags(html_text)
        await callback.answer(plain[:200], show_alert=True)
        if callback.message:
            await callback.message.edit_text(html_text, parse_mode=ParseMode.HTML, reply_markup=back_keyboard("nav:transactions"))
