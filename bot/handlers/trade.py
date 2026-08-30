from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
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
    TRASH_ID,
    WARNING_ID,
    tg_emoji,
    TG_LONG,
    TG_SHORT,
)
from bot.views import back_keyboard, bingx_chart_url, btn, fmt_money, fmt_price, format_side, get_display_snapshot
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
            [btn("1. Выбрать монету", "trade:coin", icon=DIAMOND_ID, style="primary")],
            [btn("2. Быстрое открытие", "trade:quick", icon=BOOM_ID, style="primary")],
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
        btn(f"{lev}x", f"lev:{symbol}:{budget}:{lev}", icon=GEAR_ID, style="primary")
        for lev in allowed[:5]
    ]
    row2 = [
        btn(f"{lev}x", f"lev:{symbol}:{budget}:{lev}", icon=GEAR_ID, style="primary")
        for lev in allowed[5:]
    ]
    rows = []
    if row1:
        rows.append(row1)
    if row2:
        rows.append(row2)
    rows.append([btn("Отмена", "cancel_trade", icon=CROSS_ID, style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def side_keyboard(symbol: str, budget: str, leverage: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn("LONG", f"side:{symbol}:{budget}:{leverage}:LONG", icon=LONG_EMOJI_ID, style="success"),
                btn("SHORT", f"side:{symbol}:{budget}:{leverage}:SHORT", icon=SHORT_EMOJI_ID, style="danger"),
            ],
            [btn("К плечу", f"re_lev:{symbol}:{budget}", icon=GEAR_ID, style="primary")],
            [btn("Отмена", "cancel_trade", icon=CROSS_ID, style="danger")],
        ]
    )


def tp_sl_keyboard(symbol: str, budget: str, leverage: str, side: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Установить TP/SL", f"tpsl:set:{symbol}:{budget}:{leverage}:{side}", icon=STAR_ID, style="primary")],
            [btn("Пропустить", f"tpsl:skip:{symbol}:{budget}:{leverage}:{side}", icon=FREE_ID)],
            [btn("Отмена", "cancel_trade", icon=CROSS_ID, style="danger")],
        ]
    )


def tp_sl_input_keyboard(symbol: str, budget: str, leverage: str, side: str) -> InlineKeyboardMarkup:
    """Клавиатура для ввода TP/SL: выбор ценой/процентами и одиночных."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn("Точной ценой", f"tpsl:mode:price:{symbol}:{budget}:{leverage}:{side}", icon=STAR_ID, style="primary"),
                btn("В процентах", f"tpsl:mode:percent:{symbol}:{budget}:{leverage}:{side}", icon=STAR_ID, style="primary"),
            ],
            [
                btn("Только TP", f"tpsl:only:tp:{symbol}:{budget}:{leverage}:{side}", icon=CHECK_ID, style="success"),
                btn("Только SL", f"tpsl:only:sl:{symbol}:{budget}:{leverage}:{side}", icon=CROSS_ID, style="danger"),
            ],
            [btn("Пропустить", f"tpsl:skip:{symbol}:{budget}:{leverage}:{side}", icon=FREE_ID)],
            [btn("Назад", f"tpsl:back:{symbol}:{budget}:{leverage}:{side}", icon=PIN_ID)],
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Подтвердить сделку", "trade:confirm", icon=CHECK_ID, style="success")],
            [btn("Отмена", "cancel_trade", icon=CROSS_ID, style="danger")],
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


@router.message(Command("trade", ignore_case=True))
@router.message(Command("торговать", ignore_case=True))
@router.message(Command("torgovat", ignore_case=True))
@router.message(Command("trade_ru", ignore_case=True))
@router.message(F.text == "Торговать")
async def cmd_trade(message: Message, session):
    if message.from_user is not None:
        trade_state.pop(message.from_user.id, None)
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
        raise SkipHandler
    # Не перехватываем команды и навигацию — даём другим хендлерам шанс
    if message.text.startswith("/") or message.text in ("Личный кабинет", "Сделки", "Торговать", "Топ 10", "Позиции", "Топ"):
        trade_state.pop(message.from_user.id, None)
        raise SkipHandler
    state = trade_state.get(message.from_user.id)
    if not state or "awaiting" not in state:
        # Свободный текст вне мастера — не наш апдейт, пусть идут дальше по роутерам
        raise SkipHandler
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
                        [btn("Открыть сделку", f"qsym:{symbol}", icon=BOOM_ID, style="primary")],
                        [btn("В меню торговли", "nav:trade", icon=PIN_ID)],
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

    # --- Шаг: TP/SL (поддержка ценой/процентами, одиночных) ---
    if step in ("tp_sl", "tp_sl_choice", "tp_sl_price", "tp_sl_percent", "tp_only", "sl_only", "tp_only_percent", "sl_only_percent"):
        text = (message.text or "").strip()
        if text.lower() == "skip":
            st = dict(state)
            for k in ("awaiting", "mode"):
                st.pop(k, None)
            st["tp"], st["sl"] = None, None
            trade_state[message.from_user.id] = st
            await _show_confirmation(message, st, session)
            return
        # Определяем режим: ценой или процентами, одиночный или оба
        is_percent = step in ("tp_sl_percent", "tp_only_percent", "sl_only_percent") or "%" in text
        is_tp_only = step in ("tp_only", "tp_only_percent")
        is_sl_only = step in ("sl_only", "sl_only_percent")
        # Убираем % для парсинга
        clean = text.replace("%", " ").replace(",", " ").strip()
        parts = clean.split()
        # Для одиночных — одно число, для обоих — два
        if is_tp_only or is_sl_only:
            if len(parts) != 1:
                await message.answer(f"{TG_WARNING} Введите одно число для {'TP' if is_tp_only else 'SL'}.", parse_mode=ParseMode.HTML)
                return
            try:
                val = Decimal(parts[0])
                if not val.is_finite() or val <= 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                await message.answer(f"{TG_WARNING} Значение должно быть положительным числом.", parse_mode=ParseMode.HTML)
                return
            if is_percent:
                # Рассчитать от текущей цены
                snap = await get_display_snapshot(session, state["symbol"])
                entry_est = snap.ask if state["side"] == "LONG" else snap.bid if snap else None
                if entry_est is None:
                    await message.answer(f"{TG_WARNING} Цена недоступна, введите точной ценой.", parse_mode=ParseMode.HTML)
                    return
                pct = val
                if is_tp_only:
                    tp = (entry_est * (Decimal("1") + pct/Decimal("100"))).quantize(Decimal("0.00000001")) if state["side"] == "LONG" else (entry_est * (Decimal("1") - pct/Decimal("100"))).quantize(Decimal("0.00000001"))
                    sl = None
                else:
                    sl = (entry_est * (Decimal("1") - pct/Decimal("100"))).quantize(Decimal("0.00000001")) if state["side"] == "LONG" else (entry_est * (Decimal("1") + pct/Decimal("100"))).quantize(Decimal("0.00000001"))
                    tp = None
            else:
                if is_tp_only:
                    tp, sl = val, None
                else:
                    tp, sl = None, val
        else:
            # Оба: два числа
            if len(parts) != 2:
                await message.answer(f"{TG_WARNING} Нужны два числа через пробел: TP SL. Например: 180 160 или 5% -3%", parse_mode=ParseMode.HTML)
                return
            try:
                v1, v2 = Decimal(parts[0]), Decimal(parts[1])
                if not v1.is_finite() or not v2.is_finite():
                    raise InvalidOperation
                # Для процентов — v1/v2 могут быть отрицательными для SL? Для простоты требуем положительные для ценой, для процентов - любые
                if not is_percent and (v1 <= 0 or v2 <= 0):
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                await message.answer(f"{TG_WARNING} Значения должны быть числами.", parse_mode=ParseMode.HTML)
                return
            if is_percent:
                snap = await get_display_snapshot(session, state["symbol"])
                entry_est = snap.ask if state["side"] == "LONG" else snap.bid if snap else None
                if entry_est is None:
                    await message.answer(f"{TG_WARNING} Цена недоступна, введите точной ценой.", parse_mode=ParseMode.HTML)
                    return
                # v1 = TP%, v2 = SL% (может быть отрицательным для SL? Но для простоты: TP +5, SL -3)
                # Для LONG: TP% положительный = выше, SL% положительный = ниже (передаётся как -3 или 3?)
                # Принимаем: первое — TP%, второе — SL% (SL% как положительное число — расстояние)
                # Например: 5 -3  => TP +5%, SL -3% (для LONG: TP 5% выше, SL 3% ниже)
                # Для SHORT: наоборот
                def calc(entry, pct, is_tp):
                    # pct как число: 5 => 5%, -3 => -3%
                    return (entry * (Decimal("1") + pct/Decimal("100"))).quantize(Decimal("0.00000001"))
                tp = calc(entry_est, v1, True)
                sl = calc(entry_est, v2, False)
                # Валидация: TP/SL должны быть положительными ценами
                if tp <= 0 or sl <= 0:
                    await message.answer(f"{TG_WARNING} Рассчитанные цены должны быть >0.", parse_mode=ParseMode.HTML)
                    return
            else:
                tp, sl = v1, v2
                if tp <= 0 or sl <= 0:
                    await message.answer(f"{TG_WARNING} TP и SL должны быть >0.", parse_mode=ParseMode.HTML)
                    return
        st = dict(state)
        for k in ("awaiting", "mode"):
            st.pop(k, None)
        st["tp"], st["sl"] = tp, sl
        trade_state[message.from_user.id] = st
        await _show_confirmation(message, st, session)
        return

    # --- Шаг: редактирование TP/SL открытой позиции ---
    if step.startswith("edit_"):
        # step: edit_tp_sl_price, edit_tp_sl_percent, edit_tp_only, edit_sl_only, edit_tp_only_percent, edit_sl_only_percent
        text = (message.text or "").strip()
        if text.lower() == "skip":
            # Убрать оба
            pos_id = state.get("editing_position_id")
            pos = await session.get(PaperPosition, pos_id) if pos_id else None
            if pos:
                try:
                    from services.paper_adapter import update_position_tp_sl
                    await update_position_tp_sl(session, pos, None, None)
                    await session.commit()
                    await message.answer(f"{TG_CHECK} TP/SL убраны для {pos.symbol}.", parse_mode=ParseMode.HTML)
                except Exception as exc:
                    await session.rollback()
                    await message.answer(_strip_tags(safe_trade_error(exc)), parse_mode=ParseMode.HTML)
            trade_state.pop(message.from_user.id, None)
            return
        # Определяем режим
        is_percent = "percent" in step or "%" in text
        is_tp_only = "tp_only" in step
        is_sl_only = "sl_only" in step
        clean = text.replace("%", " ").replace(",", " ").strip()
        parts = clean.split()
        pos_id = state.get("editing_position_id")
        pos = await session.get(PaperPosition, pos_id) if pos_id else None
        if not pos:
            await message.answer(f"{TG_WARNING} Позиция не найдена.", parse_mode=ParseMode.HTML)
            trade_state.pop(message.from_user.id, None)
            return
        if is_tp_only or is_sl_only:
            if len(parts) != 1:
                await message.answer(f"{TG_WARNING} Введите одно число для {'TP' if is_tp_only else 'SL'}.", parse_mode=ParseMode.HTML)
                return
            try:
                val = Decimal(parts[0])
                if not val.is_finite() or val <= 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                await message.answer(f"{TG_WARNING} Значение должно быть положительным числом.", parse_mode=ParseMode.HTML)
                return
            if is_percent:
                entry_est = pos.entry_price
                pct = val
                if is_tp_only:
                    tp = (entry_est * (Decimal("1") + pct/Decimal("100"))).quantize(Decimal("0.00000001")) if format_side(pos.side) == "LONG" else (entry_est * (Decimal("1") - pct/Decimal("100"))).quantize(Decimal("0.00000001"))
                    sl = pos.stop_loss
                else:
                    sl = (entry_est * (Decimal("1") - pct/Decimal("100"))).quantize(Decimal("0.00000001")) if format_side(pos.side) == "LONG" else (entry_est * (Decimal("1") + pct/Decimal("100"))).quantize(Decimal("0.00000001"))
                    tp = pos.take_profit
            else:
                if is_tp_only:
                    tp, sl = val, pos.stop_loss
                else:
                    tp, sl = pos.take_profit, val
        else:
            # Оба
            if len(parts) != 2:
                await message.answer(f"{TG_WARNING} Нужны два числа через пробел: TP SL.", parse_mode=ParseMode.HTML)
                return
            try:
                v1, v2 = Decimal(parts[0]), Decimal(parts[1])
                if not v1.is_finite() or not v2.is_finite():
                    raise InvalidOperation
                if not is_percent and (v1 <= 0 or v2 <= 0):
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                await message.answer(f"{TG_WARNING} Значения должны быть числами.", parse_mode=ParseMode.HTML)
                return
            if is_percent:
                entry_est = pos.entry_price
                def calc(entry, pct):
                    return (entry * (Decimal("1") + pct/Decimal("100"))).quantize(Decimal("0.00000001"))
                tp = calc(entry_est, v1)
                sl = calc(entry_est, v2)
                if tp <= 0 or sl <= 0:
                    await message.answer(f"{TG_WARNING} Рассчитанные цены должны быть >0.", parse_mode=ParseMode.HTML)
                    return
            else:
                tp, sl = v1, v2
                if tp <= 0 or sl <= 0:
                    await message.answer(f"{TG_WARNING} TP и SL должны быть >0.", parse_mode=ParseMode.HTML)
                    return
        try:
            from services.paper_adapter import update_position_tp_sl
            await update_position_tp_sl(session, pos, tp, sl)
            await session.commit()
            await message.answer(
                f"{TG_CHECK} <b>TP/SL ОБНОВЛЕНЫ</b>\n\n{pos.symbol} {format_side(pos.side)} x{pos.leverage:g}\n"
                f"TP: {fmt_price(tp) if tp else 'нет'} → SL: {fmt_price(sl) if sl else 'нет'}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("Назад", "nav:transactions:0", icon=PIN_ID)]]),
            )
        except Exception as exc:
            await session.rollback()
            await message.answer(_strip_tags(safe_trade_error(exc)), parse_mode=ParseMode.HTML)
        trade_state.pop(message.from_user.id, None)
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
    parts = callback.data.split(":")
    # tpsl:mode:price:SYMBOL:budget:leverage:side  -> 7 parts
    # tpsl:only:tp:SYMBOL:...  -> 7 parts
    # tpsl:set:SYMBOL:... / tpsl:skip:... / tpsl:back:... -> 6 parts
    try:
        if len(parts) == 7 and parts[1] == "mode":
            # tpsl:mode:price|percent:SYMBOL:budget:leverage:side
            _, _, mode, symbol, budget, leverage, side = parts
            if mode == "price":
                trade_state[callback.from_user.id] = {"symbol": symbol, "budget": budget, "leverage": leverage, "side": side, "awaiting": "tp_sl_price", "mode": "price"}
                await callback.message.edit_text(
                    f"{TG_STAR} <b>ВВЕДИТЕ TP И SL — ТОЧНОЙ ЦЕНОЙ</b>\n\nДва числа через пробел, например: 180 160\n"
                    "LONG: TP выше входа, SL ниже.\nSHORT: TP ниже входа, SL выше.\n\n"
                    "Можно одно: введите одно число (например: 180 — только TP).\n"
                    "Или напишите <code>skip</code> чтобы пропустить.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [btn("Пропустить", f"tpsl:skip:{symbol}:{budget}:{leverage}:{side}", icon=FREE_ID)],
                        [btn("Назад", f"tpsl:set:{symbol}:{budget}:{leverage}:{side}", icon=PIN_ID)],
                    ]),
                )
                await callback.answer()
                return
            elif mode == "percent":
                trade_state[callback.from_user.id] = {"symbol": symbol, "budget": budget, "leverage": leverage, "side": side, "awaiting": "tp_sl_percent", "mode": "percent"}
                await callback.message.edit_text(
                    f"{TG_STAR} <b>ВВЕДИТЕ TP И SL — В ПРОЦЕНТАХ</b>\n\nДва числа через пробел, например: 5 -3  (TP +5%, SL -3%)\n"
                    "Можно с %: 5% -3%  или  +5  -3\n"
                    "LONG: TP +, SL - ; SHORT: наоборот\n"
                    "Можно одно: 5 (только TP) или -3 (только SL)\n"
                    "Или <code>skip</code>.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [btn("Пропустить", f"tpsl:skip:{symbol}:{budget}:{leverage}:{side}", icon=FREE_ID)],
                        [btn("Назад", f"tpsl:set:{symbol}:{budget}:{leverage}:{side}", icon=PIN_ID)],
                    ]),
                )
                await callback.answer()
                return
        if len(parts) == 7 and parts[1] == "only":
            # tpsl:only:tp|sl:SYMBOL:budget:leverage:side
            _, _, only, symbol, budget, leverage, side = parts
            if only == "tp":
                trade_state[callback.from_user.id] = {"symbol": symbol, "budget": budget, "leverage": leverage, "side": side, "awaiting": "tp_only", "mode": "price"}
                await callback.message.edit_text(
                    f"{TG_STAR} <b>ТОЛЬКО TP</b>\n\nВведите цену TP точной ценой, например: 180\n"
                    f"Или в процентах: 5% (будет рассчитано от входа)\n"
                    f"Для LONG TP выше входа, для SHORT — ниже.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [btn("В процентах", f"tpsl:only:tp_percent:{symbol}:{budget}:{leverage}:{side}", icon=STAR_ID)],
                        [btn("Пропустить", f"tpsl:skip:{symbol}:{budget}:{leverage}:{side}", icon=FREE_ID)],
                        [btn("Назад", f"tpsl:set:{symbol}:{budget}:{leverage}:{side}", icon=PIN_ID)],
                    ]),
                )
                await callback.answer()
                return
            elif only == "sl":
                trade_state[callback.from_user.id] = {"symbol": symbol, "budget": budget, "leverage": leverage, "side": side, "awaiting": "sl_only", "mode": "price"}
                await callback.message.edit_text(
                    f"{TG_STAR} <b>ТОЛЬКО SL</b>\n\nВведите цену SL точной ценой, например: 160\n"
                    f"Или в процентах: -5% (будет рассчитано)\n"
                    f"Для LONG SL ниже входа, для SHORT — выше.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [btn("В процентах", f"tpsl:only:sl_percent:{symbol}:{budget}:{leverage}:{side}", icon=STAR_ID)],
                        [btn("Пропустить", f"tpsl:skip:{symbol}:{budget}:{leverage}:{side}", icon=FREE_ID)],
                        [btn("Назад", f"tpsl:set:{symbol}:{budget}:{leverage}:{side}", icon=PIN_ID)],
                    ]),
                )
                await callback.answer()
                return
            elif only in ("tp_percent", "sl_percent"):
                is_tp = "tp" in only
                trade_state[callback.from_user.id] = {"symbol": symbol, "budget": budget, "leverage": leverage, "side": side, "awaiting": "tp_only_percent" if is_tp else "sl_only_percent", "mode": "percent"}
                await callback.message.edit_text(
                    f"{TG_STAR} <b>ТОЛЬКО {'TP' if is_tp else 'SL'} — В ПРОЦЕНТАХ</b>\n\nВведите процент, например: 5  или  -3%\n"
                    f"{'LONG TP +' if (is_tp and side=='LONG') or (not is_tp and side=='SHORT') else 'LONG SL -'} / {'SHORT TP -' if is_tp else 'SHORT SL +'}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [btn("Точной ценой", f"tpsl:only:{'tp' if is_tp else 'sl'}:{symbol}:{budget}:{leverage}:{side}", icon=STAR_ID)],
                        [btn("Пропустить", f"tpsl:skip:{symbol}:{budget}:{leverage}:{side}", icon=FREE_ID)],
                        [btn("Назад", f"tpsl:set:{symbol}:{budget}:{leverage}:{side}", icon=PIN_ID)],
                    ]),
                )
                await callback.answer()
                return
        # Fallback for 6-part: tpsl:back|skip|set
        action, symbol, budget, leverage, side = parts[1:6] if len(parts) >= 6 else (None, None, None, None, None)
        if len(parts) < 6:
            raise ValueError()
    except ValueError:
        await callback.answer("Некорректные данные", show_alert=True)
        return
    if action == "back":
        # Назад к селектору TP/SL (не к подтверждению)
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
        return
    # Показываем выбор: ценой/процентами, оба или одиночный, пропустить
    trade_state[callback.from_user.id] = {
        "symbol": symbol,
        "budget": budget,
        "leverage": leverage,
        "side": side,
        "awaiting": "tp_sl_choice",
    }
    await callback.message.edit_text(
        f"{TG_STAR} <b>TP/SL — ВЫБЕРИТЕ РЕЖИМ</b>\n\n"
        f"Пара: {symbol} | {side}\n\n"
        f"Как установить уровни?\n"
        f"• <b>Точной ценой</b> — например: 180 160\n"
        f"• <b>В процентах</b> — например: 5% -3% (от входа)\n"
        f"• <b>Только TP/SL</b> — один уровень\n\n"
        f"LONG: TP выше входа, SL ниже.\nSHORT: TP ниже входа, SL выше.",
        parse_mode=ParseMode.HTML,
        reply_markup=tp_sl_input_keyboard(symbol, budget, leverage, side),
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
                    [btn("Мои сделки", "nav:transactions", icon=CHART_ID, style="primary")],
                    [btn("Торговать", "nav:trade", icon=CHART_UP_ID, style="success")],
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
                [btn("Да, закрыть", f"close_confirm:{position.id}", icon=CHECK_ID, style="danger")],
                [btn("Отмена", "nav:transactions", icon=CROSS_ID)],
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
                inline_keyboard=[[btn("Мои сделки", "nav:transactions", icon=CHART_ID, style="primary")]]
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


@router.callback_query(F.data.regexp(r"^edit_tp_sl:\d+$"))
async def cb_edit_tp_sl(callback: CallbackQuery, session):
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
    # Сохраняем position_id для редактирования
    trade_state[callback.from_user.id] = {
        "editing_position_id": pos_id,
        "symbol": position.symbol,
        "side": format_side(position.side),
        "awaiting": "edit_tp_sl_choice",
    }
    current_tp = fmt_price(position.take_profit) if position.take_profit else "нет"
    current_sl = fmt_price(position.stop_loss) if position.stop_loss else "нет"
    await callback.message.edit_text(
        f"{TG_STAR} <b>РЕДАКТОР TP/SL</b>\n\n"
        f"{position.symbol} {TG_LONG if format_side(position.side) == 'LONG' else TG_SHORT} {format_side(position.side)} x{position.leverage:g}\n"
        f"Вход: {fmt_price(position.entry_price)} → Сейчас: {fmt_price(position.current_price)}\n"
        f"Текущий TP: {current_tp}\n"
        f"Текущий SL: {current_sl}\n\n"
        f"Выберите как изменить:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("Точной ценой", f"edit_tp_sl:mode:price:{pos_id}", icon=STAR_ID, style="primary"), btn("В процентах", f"edit_tp_sl:mode:percent:{pos_id}", icon=STAR_ID, style="primary")],
                [btn("Только TP", f"edit_tp_sl:only:tp:{pos_id}", icon=CHECK_ID, style="success"), btn("Только SL", f"edit_tp_sl:only:sl:{pos_id}", icon=CROSS_ID, style="danger")],
                [btn("Убрать TP/SL", f"edit_tp_sl:clear:{pos_id}", icon=TRASH_ID, style="danger")],
                [btn("Назад", f"nav:transactions:0", icon=PIN_ID)],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit_tp_sl:mode:"))
async def cb_edit_tp_sl_mode(callback: CallbackQuery, session):
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    try:
        _, _, mode, pos_id_str = callback.data.split(":", 3)
        pos_id = int(pos_id_str)
    except ValueError:
        await callback.answer("Некорректные данные", show_alert=True)
        return
    state = trade_state.get(callback.from_user.id, {})
    if not state.get("editing_position_id") or state["editing_position_id"] != pos_id:
        # Инициализируем если нет
        pos = await session.get(PaperPosition, pos_id)
        if not pos:
            await callback.answer("Позиция не найдена", show_alert=True)
            return
        trade_state[callback.from_user.id] = {"editing_position_id": pos_id, "symbol": pos.symbol, "side": format_side(pos.side), "awaiting": f"edit_tp_sl_{mode}", "mode": mode}
    else:
        trade_state[callback.from_user.id]["awaiting"] = f"edit_tp_sl_{mode}"
        trade_state[callback.from_user.id]["mode"] = mode
    await callback.message.edit_text(
        f"{TG_STAR} <b>ВВЕДИТЕ TP И SL — {'ТОЧНОЙ ЦЕНОЙ' if mode == 'price' else 'В ПРОЦЕНТАХ'}</b>\n\n"
        f"Два числа через пробел, например: {'180 160' if mode == 'price' else '5 -3  (TP +5%, SL -3%)'}\n"
        f"Можно одно: {'180 — только TP' if mode == 'price' else '5 — только TP'}\n"
        f"Или <code>skip</code> чтобы убрать оба.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("Назад", f"edit_tp_sl:{pos_id}", icon=PIN_ID)],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit_tp_sl:only:"))
async def cb_edit_tp_sl_only(callback: CallbackQuery, session):
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    try:
        _, _, only, pos_id_str = callback.data.split(":", 3)
        pos_id = int(pos_id_str)
        # Handle tp_percent/sl_percent variants
        if only in ("tp_percent", "sl_percent"):
            is_tp = "tp" in only
            mode = "percent"
            awaiting = "edit_tp_only_percent" if is_tp else "edit_sl_only_percent"
        else:
            is_tp = only == "tp"
            mode = "price"
            awaiting = "edit_tp_only" if is_tp else "edit_sl_only"
    except ValueError:
        await callback.answer("Некорректные данные", show_alert=True)
        return
    pos = await session.get(PaperPosition, pos_id)
    if not pos:
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    trade_state[callback.from_user.id] = {"editing_position_id": pos_id, "symbol": pos.symbol, "side": format_side(pos.side), "awaiting": awaiting, "mode": mode}
    await callback.message.edit_text(
        f"{TG_STAR} <b>ТОЛЬКО {'TP' if is_tp else 'SL'} — {'В ПРОЦЕНТАХ' if 'percent' in awaiting else 'ТОЧНОЙ ЦЕНОЙ'}</b>\n\n"
        f"Введите {'цену' if 'price' in awaiting or awaiting in ('edit_tp_only','edit_sl_only') else 'процент'} {'TP' if is_tp else 'SL'}, например: {'180' if 'price' in awaiting else '5%'}\n"
        f"Или <code>skip</code> чтобы убрать.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("В процентах" if 'price' in awaiting else "Точной ценой", f"edit_tp_sl:only:{'tp_percent' if is_tp else 'sl_percent'}:{pos_id}" if 'price' in awaiting else f"edit_tp_sl:only:{'tp' if is_tp else 'sl'}:{pos_id}", icon=STAR_ID)],
            [btn("Назад", f"edit_tp_sl:{pos_id}", icon=PIN_ID)],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit_tp_sl:clear:"))
async def cb_edit_tp_sl_clear(callback: CallbackQuery, session):
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    try:
        pos_id = int(callback.data.split(":")[2])
    except ValueError:
        await callback.answer("Некорректные данные", show_alert=True)
        return
    user = (await session.execute(select(User).where(User.telegram_id == callback.from_user.id))).scalar_one_or_none()
    account = (await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none() if user else None
    position = await session.get(PaperPosition, pos_id) if account else None
    if not position or position.account_id != account.id:
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    try:
        from services.paper_adapter import update_position_tp_sl
        await update_position_tp_sl(session, position, None, None)
        await session.commit()
        await callback.message.edit_text(
            f"{TG_CHECK} <b>TP/SL УБРАНЫ</b>\n\n{position.symbol} {format_side(position.side)} x{position.leverage:g}\n"
            f"Теперь без TP/SL.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("Назад", "nav:transactions:0", icon=PIN_ID)]]),
        )
        await callback.answer("TP/SL убраны")
    except Exception as exc:
        await session.rollback()
        html_text = safe_trade_error(exc)
        await callback.answer(_strip_tags(html_text)[:200], show_alert=True)
        if callback.message:
            await callback.message.edit_text(html_text, parse_mode=ParseMode.HTML)
