from __future__ import annotations

import html
from decimal import Decimal

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from bot.emojis import (
    CHART_ID,
    CHART_UP_ID,
    CROSS_ID,
    CROWN_ID,
    GOLD_ID,
    GREEN_ID,
    MONEY_ID,
    PARTY_ID,
    PIN_ID,
    RED_ID,
    SIREN_ID,
    STAR_ID,
    TG_CHART,
    TG_CHART_UP,
    TG_CHECK,
    TG_CROWN,
    TG_GREEN,
    TG_LONG,
    TG_MONEY,
    TG_PARTY,
    TG_RED,
    TG_SHORT,
    tg_emoji,
)
from bot.keyboards import contact_keyboard
from bot.views import (
    btn,
    fmt_leverage,
    fmt_leverage_move_pct,
    fmt_money,
    fmt_pct,
    fmt_price,
    format_side,
    main_menu,
    safe_edit,
)
from db.models import User
from db.paper_models import PaperPosition, PositionStatus, TradingAccount
from services.accounts import get_or_create_user, verify_phone
from services.pnl import (
    cross_liquidation_buffer,
    cross_liquidation_buffer_pct,
    cross_liquidation_threshold,
)
from services.competition import get_or_create_default_competition, join_competition
from services.leaderboard import get_user_rank
from services.trading_account import get_or_create_trading_account

# trade_state — очищаем при навигации вне торговли, чтобы не hijack-ить следующую команду
try:
    from bot.handlers.trade import trade_state as _trade_state  # type: ignore
except ImportError:
    _trade_state = {}

router = Router()

# Local premium tag not in emojis.py
TG_STAR = tg_emoji(STAR_ID, "⭐️")


async def _get_user_by_telegram_id(session, telegram_id: int) -> User | None:
    return (
        await session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one_or_none()


async def _grant_demo_balance(session, user: User):
    """Идемпотентный демо-грант $10 000 (один счёт на пользователя)."""
    return await get_or_create_trading_account(session, user.id)


async def _ensure_competition(session, user: User):
    competition = await get_or_create_default_competition(session)
    await join_competition(session, user.id, competition.id)
    return competition


async def send_main_menu(message: Message, text: str = "Главное меню:"):
    await message.answer(text, reply_markup=main_menu())


@router.message(Command("start", ignore_case=True))
async def cmd_start(message: Message, session):
    if message.from_user is None:
        return
    _trade_state.pop(message.from_user.id, None)
    user = await _get_user_by_telegram_id(session, message.from_user.id)
    is_new = user is None
    if user is None:
        user = User(telegram_id=message.from_user.id, username=message.from_user.username)
        session.add(user)
        await session.flush()

    if user.phone_verified_at is None:
        await session.commit()
        await message.answer(
            f"Привет! Это <b>демо-тренажёр</b> криптотрейдинга с реальными ценами BingX.\n"
            f"Все деньги виртуальные — это <b>не биржа</b>, риск потери реальных средств отсутствует.\n\n"
            f"Для старта подтвердите номер телефона — на него будет записан демо-баланс $10 000.",
            reply_markup=contact_keyboard(),
        )
        return

    await _grant_demo_balance(session, user)
    await _ensure_competition(session, user)
    await session.commit()
    welcome = f"{TG_PARTY} Добро пожаловать!" if is_new else "С возвращением!"
    await send_main_menu(
        message,
        f"{welcome}\n\n"
        f"{TG_MONEY} Демо-баланс: $10 000\n\n"
        f"{tg_emoji(CHART_UP_ID, '📈')} <b>Торговать</b> — открыть сделку\n"
        f"{TG_CROWN} <b>Личный кабинет</b> — баланс, сделки, рейтинг",
    )


@router.message(F.contact)
async def handle_contact(message: Message, session):
    if message.from_user is None or message.contact is None:
        return
    contact = message.contact
    if contact.user_id != message.from_user.id:
        await message.answer("Поделитесь своим номером: нажмите кнопку ниже.", reply_markup=contact_keyboard())
        return

    user = await _get_user_by_telegram_id(session, message.from_user.id)
    if user is None:
        user = User(telegram_id=message.from_user.id, username=message.from_user.username)
        session.add(user)
        await session.flush()

    try:
        await verify_phone(session, user, contact.phone_number)
        account = await _grant_demo_balance(session, user)
        await _ensure_competition(session, user)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        if "uq" in str(exc).lower() or "unique" in str(exc).lower():
            await message.answer("Этот номер уже зарегистрирован на другом аккаунте.")
        else:
            await message.answer("Не удалось подтвердить номер. Попробуйте ещё раз: /start")
        return

    await send_main_menu(
        message,
        f"{TG_CHECK} Номер подтверждён: {contact.phone_number}\n\n"
        f"{TG_MONEY} Демо-баланс начислен: {fmt_money(account.initial_balance)}\n\n"
        f"{tg_emoji(CHART_UP_ID, '📈')} <b>Торговать</b> — открыть сделку\n"
        f"{TG_CROWN} <b>Личный кабинет</b> — баланс, сделки, рейтинг",
    )


@router.message(Command("profile", ignore_case=True))
@router.message(Command("profil", ignore_case=True))
@router.message(Command("профиль", ignore_case=True))
@router.message(Command("личный_кабинет", ignore_case=True))
@router.message(F.text == "Личный кабинет")
async def cmd_profile(message: Message, session):
    if message.from_user is None:
        return
    _trade_state.pop(message.from_user.id, None)
    await _send_profile(message.from_user.id, session, message)


@router.message(Command("transactions", ignore_case=True))
@router.message(Command("sdelki", ignore_case=True))
@router.message(Command("сделки", ignore_case=True))
@router.message(Command("активные", ignore_case=True))
@router.message(Command("actives", ignore_case=True))
@router.message(F.text.in_({"Сделки", "Мои сделки", "Активные сделки"}))
async def cmd_transactions(message: Message, session):
    if message.from_user is None:
        return
    _trade_state.pop(message.from_user.id, None)
    await _send_transactions(message.from_user.id, session, message)


@router.message(Command("history", ignore_case=True))
@router.message(Command("история", ignore_case=True))
@router.message(Command("istoriya", ignore_case=True))
@router.message(Command("все_сделки", ignore_case=True))
@router.message(Command("vse_sdelki", ignore_case=True))
@router.message(F.text == "Посмотреть все сделки")
async def cmd_history(message: Message, session):
    if message.from_user is None:
        return
    _trade_state.pop(message.from_user.id, None)
    await _send_history(message.from_user.id, session, message)


async def _send_profile(telegram_id: int, session, target: Message | CallbackQuery):
    """Shared profile renderer for message and callback."""
    is_callback = isinstance(target, CallbackQuery)
    chat_target = target.message if is_callback else target
    if chat_target is None:
        if is_callback:
            await target.answer()
        return
    user = await _get_user_by_telegram_id(session, telegram_id)
    if not user:
        await chat_target.answer("Сначала отправьте /start")
        if is_callback:
            await target.answer()
        return
    if user.phone_verified_at is None:
        await chat_target.answer("Сначала подтвердите номер телефона: /start", reply_markup=contact_keyboard())
        if is_callback:
            await target.answer()
        return
    account = (
        await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    ).scalar_one_or_none()
    if not account:
        await chat_target.answer("Профиль ещё не создан. Отправьте /start")
        if is_callback:
            await target.answer()
        return

    positions = (
        await session.execute(select(PaperPosition).where(PaperPosition.account_id == account.id))
    ).scalars().all()
    closed = [p for p in positions if p.status == PositionStatus.CLOSED.value]
    wins = len([p for p in closed if p.realized_pnl > 0])
    losses = len(closed) - wins

    competition = await get_or_create_default_competition(session)
    rank_info = await get_user_rank(session, competition.id, user.id)
    rank = f"#{rank_info['rank']}" if rank_info else "—"
    roe = fmt_pct(rank_info["roi"]) if rank_info else "+0.00%"

    safe_name = html.escape(str(user.username or user.telegram_id))[:32]
    text = (
        f"{TG_CROWN} <b>ЛИЧНЫЙ КАБИНЕТ</b>\n\n"
        f"{tg_emoji(PIN_ID, '📌')} Юзернейм: {safe_name}\n"
        f"{TG_MONEY} Баланс: {fmt_money(account.equity)}\n\n"
        f"{TG_CHART} <b>СДЕЛКИ</b>\n"
        f"Успешных: {wins}\n"
        f"Неуспешных: {losses}\n\n"
        f"{tg_emoji(CHART_UP_ID, '📈')} Общий ROE: {roe}\n"
        f"{TG_STAR} Место в рейтинге: {rank}"
    )
    await chat_target.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("Сделки", "nav:transactions", icon=GREEN_ID, style="primary")],
                [btn("Список лидеров", "nav:top", icon=GOLD_ID, style="primary")],
                [btn("Торговать", "nav:trade", icon=CHART_UP_ID, style="success")],
            ]
        ),
    )
    if is_callback:
        await target.answer()


async def _send_transactions(telegram_id: int, session, target: Message | CallbackQuery, offset: int = 0):
    """Активные сделки только. Внизу кнопка 'Посмотреть все сделки' -> история закрытых со статистикой."""
    is_callback = isinstance(target, CallbackQuery)
    chat_target = target.message if is_callback else target
    if chat_target is None:
        if is_callback:
            await target.answer()
        return
    user = await _get_user_by_telegram_id(session, telegram_id)
    if not user:
        await chat_target.answer("Сначала отправьте /start")
        if is_callback:
            await target.answer()
        return
    account = (
        await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    ).scalar_one_or_none()
    if not account:
        await chat_target.answer("Сделок пока нет. Нажмите Торговать.", reply_markup=main_menu())
        if is_callback:
            await target.answer()
        return
    from sqlalchemy import func

    total_active = (await session.execute(select(func.count()).select_from(PaperPosition).where(PaperPosition.account_id == account.id, PaperPosition.status == PositionStatus.OPEN.value))).scalar_one()
    limit = 5
    positions = (
        await session.execute(
            select(PaperPosition)
            .where(PaperPosition.account_id == account.id, PaperPosition.status == PositionStatus.OPEN.value)
            .order_by(PaperPosition.opened_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    if not positions and offset == 0:
        # Нет активных — показать пусто и кнопку на историю
        kb_empty = InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("Посмотреть все сделки", "nav:history:0", icon=CHART_ID, style="primary")],
            ]
        )
        await chat_target.answer(
            f"{TG_CHART} <b>АКТИВНЫЕ СДЕЛКИ</b>\n\nАктивных сделок нет.\n\nНажмите Торговать, чтобы открыть первую.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_empty,
        )
        if is_callback:
            await target.answer()
        return
    if not positions and offset > 0:
        await chat_target.answer("Больше активных сделок нет.")
        if is_callback:
            await target.answer()
        return

    kb_rows = [
        [
            btn(f"Закрыть {p.symbol} {format_side(p.side)}", f"close_preview:{p.id}", icon=RED_ID, style="danger"),
            btn("TP/SL", f"edit_tp_sl:{p.id}", icon=STAR_ID, style="primary"),
        ]
        for p in positions
    ]
    pag_row = []
    if offset > 0:
        pag_row.append(btn("◀ Назад", f"nav:transactions:{max(0, offset - limit)}", icon=PIN_ID))
    if offset + limit < total_active:
        pag_row.append(btn("Ещё ▶", f"nav:transactions:{offset + limit}", icon=PIN_ID))
    if pag_row:
        kb_rows.append(pag_row)
    kb_rows.append([btn("Обновить", f"nav:transactions:{offset}", icon=GREEN_ID, style="success")])
    kb_rows.append([btn("Посмотреть все сделки", "nav:history:0", icon=CHART_ID, style="primary")])
    open_keyboard = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    page_num = offset // limit + 1
    total_pages = (total_active + limit - 1) // limit if total_active else 1
    # Кросс-маржа: запас до ликвидации на уровне аккаунта (общий для всех позиций)
    threshold = cross_liquidation_threshold(account.initial_balance)
    buffer = cross_liquidation_buffer(account.equity, account.initial_balance)
    buffer_pct = cross_liquidation_buffer_pct(account.equity, account.initial_balance)
    header = f"{TG_CHART} <b>АКТИВНЫЕ СДЕЛКИ</b> {page_num}/{total_pages} — всего {total_active}\n"
    header += f"{tg_emoji(SIREN_ID, '🚨')} Запас до ликвидации: {fmt_money(buffer)} ({buffer_pct}%) | Порог equity {fmt_money(threshold)}\n"
    lines = [header]
    for p in positions:
        side_str = format_side(p.side)
        side_tag = TG_LONG if side_str == "LONG" else TG_SHORT
        pnl_line = f"PnL: {fmt_money(p.unrealized_pnl)}"
        # Сумма входа = маржа = бюджет, который юзер реально выделил (notional/leverage)
        pos_margin = (p.notional / p.leverage) if p.leverage else p.notional
        pnl_pct = (p.unrealized_pnl / pos_margin * 100) if pos_margin else Decimal("0")
        pnl_pct_str = fmt_pct(pnl_pct)
        status_line = f"{tg_emoji(GREEN_ID, '🟢')} ОТКРЫТА"
        lines.append(
            f"{p.symbol} {side_tag} {side_str} {fmt_leverage(p.leverage)}\n"
            f"Вход: {fmt_price(p.entry_price)} → Сейчас: {fmt_price(p.current_price)}\n"
            f"Вход (маржа): {fmt_money(pos_margin)} | {pnl_line} ({pnl_pct_str})\n"
            f"Объём с плечом: {fmt_money(p.notional)}\n"
            f"{status_line}"
        )
    # Callback («Обновить»/навигация) — редактируем окно на месте, не создавая новое сообщение
    if is_callback:
        await safe_edit(chat_target, "\n\n".join(lines), markup=open_keyboard, parse_mode=ParseMode.HTML)
        await target.answer()
    else:
        await chat_target.answer("\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=open_keyboard)


async def _send_history(telegram_id: int, session, target: Message | CallbackQuery, offset: int = 0):
    """История закрытых сделок + статистика: проценты, + и - бюджета."""
    is_callback = isinstance(target, CallbackQuery)
    chat_target = target.message if is_callback else target
    if chat_target is None:
        if is_callback:
            await target.answer()
        return
    user = await _get_user_by_telegram_id(session, telegram_id)
    if not user:
        await chat_target.answer("Сначала отправьте /start")
        if is_callback:
            await target.answer()
        return
    account = (
        await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    ).scalar_one_or_none()
    if not account:
        await chat_target.answer("История пуста.", reply_markup=main_menu())
        if is_callback:
            await target.answer()
        return
    from sqlalchemy import func

    # Статистика по закрытым — SQL-агрегаты одним запросом (не грузим все строки)
    stats = (
        await session.execute(
            select(
                func.count(PaperPosition.id),
                func.coalesce(func.sum(PaperPosition.realized_pnl), 0),
                func.max(PaperPosition.realized_pnl),
                func.min(PaperPosition.realized_pnl),
            ).where(PaperPosition.account_id == account.id, PaperPosition.status == PositionStatus.CLOSED.value)
        )
    ).one()
    total_closed, total_pnl_raw, best_raw, worst_raw = stats
    total_closed = int(total_closed or 0)
    total_pnl = Decimal(str(total_pnl_raw)).quantize(Decimal("0.01"))
    best = Decimal(str(best_raw)) if best_raw is not None else Decimal("0")
    worst = Decimal(str(worst_raw)) if worst_raw is not None else Decimal("0")
    if total_closed == 0:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("Активные сделки", "nav:transactions:0", icon=CHART_ID, style="primary")],
                [btn("Торговать", "nav:trade", icon=CHART_UP_ID, style="success")],
            ]
        )
        await chat_target.answer(
            f"{TG_CHART} <b>ИСТОРИЯ СДЕЛОК</b>\n\nЗакрытых сделок пока нет.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        if is_callback:
            await target.answer()
        return

    # Счётчик успешных/неуспешных — вторым лёгким запросом
    win_cnt = int((await session.execute(
        select(func.count(PaperPosition.id)).where(
            PaperPosition.account_id == account.id,
            PaperPosition.status == PositionStatus.CLOSED.value,
            PaperPosition.realized_pnl > 0,
        )
    )).scalar_one())
    loss_cnt = total_closed - win_cnt
    win_rate = (win_cnt / total_closed * 100) if total_closed else 0
    # Плюс/минус бюджета: сумма pnl по выигравшим и проигравшим
    plus_raw = (await session.execute(
        select(func.coalesce(func.sum(PaperPosition.realized_pnl), 0)).where(
            PaperPosition.account_id == account.id,
            PaperPosition.status == PositionStatus.CLOSED.value,
            PaperPosition.realized_pnl > 0,
        )
    )).scalar_one()
    minus_raw = (await session.execute(
        select(func.coalesce(func.sum(PaperPosition.realized_pnl), 0)).where(
            PaperPosition.account_id == account.id,
            PaperPosition.status == PositionStatus.CLOSED.value,
            PaperPosition.realized_pnl <= 0,
        )
    )).scalar_one()
    plus = Decimal(str(plus_raw)).quantize(Decimal("0.01"))
    minus = Decimal(str(minus_raw)).quantize(Decimal("0.01"))

    limit = 5
    positions = (
        await session.execute(
            select(PaperPosition)
            .where(PaperPosition.account_id == account.id, PaperPosition.status == PositionStatus.CLOSED.value)
            .order_by(PaperPosition.closed_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    # Клавиатура пагинации
    kb_rows = []
    pag_row = []
    if offset > 0:
        pag_row.append(btn("◀ Назад", f"nav:history:{max(0, offset - limit)}", icon=PIN_ID))
    if offset + limit < total_closed:
        pag_row.append(btn("Ещё ▶", f"nav:history:{offset + limit}", icon=PIN_ID))
    if pag_row:
        kb_rows.append(pag_row)
    kb_rows.append([btn("Обновить", f"nav:history:{offset}", icon=GREEN_ID, style="success")])
    kb_rows.append([btn("Активные сделки", "nav:transactions:0", icon=CHART_ID, style="primary")])
    kb_rows.append([btn("Список лидеров", "nav:top", icon=GOLD_ID, style="primary")])

    page_num = offset // limit + 1
    total_pages = (total_closed + limit - 1) // limit
    header = (
        f"{TG_CHART} <b>ИСТОРИЯ — ЗАКРЫТЫЕ СДЕЛКИ</b> {page_num}/{total_pages}\n"
        f"Всего: {total_closed}  {tg_emoji(GREEN_ID, '🟢')} Успешных: {win_cnt} ({win_rate:.1f}%)  {tg_emoji(RED_ID, '🔴')} Неуспешных: {loss_cnt}\n"
        f"{TG_MONEY} Общий PnL: {fmt_money(total_pnl)}  {tg_emoji(GREEN_ID, '🟢')} +{fmt_money(plus)}  {tg_emoji(RED_ID, '🔴')} {fmt_money(minus)}\n"
        f"Лучшая: {fmt_money(best)}  Худшая: {fmt_money(worst)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )
    lines = [header]
    for p in positions:
        side_str = format_side(p.side)
        side_tag = TG_LONG if side_str == "LONG" else TG_SHORT
        pnl = p.realized_pnl
        pnl_emoji = tg_emoji(GREEN_ID, "🟢") if pnl > 0 else tg_emoji(RED_ID, "🔴")
        lines.append(
            f"{p.symbol} {side_tag} {side_str} {fmt_leverage(p.leverage or 1)} {pnl_emoji} {fmt_money(pnl)}\n"
            f"Вход: {fmt_price(p.entry_price)} → Выход: {fmt_price(p.current_price)}  {fmt_pct((pnl / (p.notional / (p.leverage or 1)) * 100) if p.notional else 0)}\n"
            f"{p.closed_at.strftime('%d.%m %H:%M') if p.closed_at else ''}"
        )
    # Callback («Обновить»/пагинация) — редактируем окно на месте
    if is_callback:
        await safe_edit(chat_target, "\n\n".join(lines), markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode=ParseMode.HTML)
        await target.answer()
    else:
        await chat_target.answer("\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data == "nav:home")
async def nav_home(callback: CallbackQuery, session):
    if callback.from_user is not None:
        _trade_state.pop(callback.from_user.id, None)
    if callback.message is None:
        await callback.answer()
        return
    await callback.message.answer("Главное меню:", reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("nav:transactions"))
async def nav_transactions(callback: CallbackQuery, session):
    if callback.from_user is None:
        await callback.answer()
        return
    _trade_state.pop(callback.from_user.id, None)
    # Parse offset: nav:transactions or nav:transactions:5
    offset = 0
    parts = callback.data.split(":")
    if len(parts) == 3:
        try:
            offset = int(parts[2])
        except ValueError:
            offset = 0
    await _send_transactions(callback.from_user.id, session, callback, offset=offset)


@router.callback_query(F.data.startswith("nav:history"))
async def nav_history(callback: CallbackQuery, session):
    if callback.from_user is None:
        await callback.answer()
        return
    _trade_state.pop(callback.from_user.id, None)
    offset = 0
    parts = callback.data.split(":")
    if len(parts) == 3:
        try:
            offset = int(parts[2])
        except ValueError:
            offset = 0
    await _send_history(callback.from_user.id, session, callback, offset=offset)
