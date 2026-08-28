from __future__ import annotations

import html

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
from bot.views import fmt_money, fmt_pct, fmt_price, format_side, main_menu
from db.models import User
from db.paper_models import PaperPosition, PositionStatus, TradingAccount
from services.accounts import get_or_create_user, verify_phone
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


@router.message(Command("start"))
async def cmd_start(message: Message, session):
    if message.from_user is None:
        return
    user = await _get_user_by_telegram_id(session, message.from_user.id)
    is_new = user is None
    if user is None:
        user = User(telegram_id=message.from_user.id, username=message.from_user.username)
        session.add(user)
        await session.flush()

    if user.phone_verified_at is None:
        await session.commit()
        await message.answer(
            f"Привет! Это демо-тренажёр криптотрейдинга с реальными ценами BingX.\n\n"
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


@router.message(Command("profile"))
@router.message(Command("profil"))
@router.message(Command("профиль"))
@router.message(Command("личный_кабинет"))
@router.message(F.text == "Личный кабинет")
async def cmd_profile(message: Message, session):
    if message.from_user is None:
        return
    _trade_state.pop(message.from_user.id, None)
    await _send_profile(message.from_user.id, session, message)


@router.message(Command("transactions"))
@router.message(Command("sdelki"))
@router.message(Command("сделки"))
@router.message(Command("активные"))
@router.message(Command("actives"))
@router.message(F.text.in_({"Сделки", "Мои сделки", "Активные сделки"}))
async def cmd_transactions(message: Message, session):
    if message.from_user is None:
        return
    _trade_state.pop(message.from_user.id, None)
    await _send_transactions(message.from_user.id, session, message)


@router.message(Command("history"))
@router.message(Command("история"))
@router.message(Command("istoriya"))
@router.message(Command("все_сделки"))
@router.message(Command("vse_sdelki"))
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
                [InlineKeyboardButton(text="Сделки", callback_data="nav:transactions", icon_custom_emoji_id=CHART_ID)],
                [InlineKeyboardButton(text="Топ 10", callback_data="nav:top", icon_custom_emoji_id=GOLD_ID)],
                [InlineKeyboardButton(text="Торговать", callback_data="nav:trade", icon_custom_emoji_id=CHART_UP_ID)],
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
                [InlineKeyboardButton(text="Посмотреть все сделки", callback_data="nav:history:0", icon_custom_emoji_id=CHART_ID)],
                [InlineKeyboardButton(text="Торговать", callback_data="nav:trade", icon_custom_emoji_id=CHART_UP_ID)],
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
            InlineKeyboardButton(
                text=f"Закрыть {p.symbol} {format_side(p.side)}",
                callback_data=f"close_preview:{p.id}",
                icon_custom_emoji_id=RED_ID,
            )
        ]
        for p in positions
    ]
    pag_row = []
    if offset > 0:
        pag_row.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"nav:transactions:{max(0, offset - limit)}", icon_custom_emoji_id=PIN_ID))
    if offset + limit < total_active:
        pag_row.append(InlineKeyboardButton(text="Ещё ▶", callback_data=f"nav:transactions:{offset + limit}", icon_custom_emoji_id=PIN_ID))
    if pag_row:
        kb_rows.append(pag_row)
    # Кнопка на все сделки (история закрытых)
    kb_rows.append([InlineKeyboardButton(text="Посмотреть все сделки", callback_data="nav:history:0", icon_custom_emoji_id=CHART_ID)])
    kb_rows.append([InlineKeyboardButton(text="Торговать", callback_data="nav:trade", icon_custom_emoji_id=CHART_UP_ID)])
    open_keyboard = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    page_num = offset // limit + 1
    total_pages = (total_active + limit - 1) // limit if total_active else 1
    lines = [f"{TG_CHART} <b>АКТИВНЫЕ СДЕЛКИ</b> {page_num}/{total_pages} — всего {total_active}\n"]
    for p in positions:
        side_str = format_side(p.side)
        side_tag = TG_LONG if side_str == "LONG" else TG_SHORT
        pnl_line = f"PnL: {fmt_money(p.unrealized_pnl)}"
        status_line = f"{tg_emoji(GREEN_ID, '🟢')} ОТКРЫТА"
        lines.append(
            f"{p.symbol} {side_tag} {side_str} x{p.leverage or 1:g}\n"
            f"Вход: {fmt_price(p.entry_price)} → Сейчас: {fmt_price(p.current_price)}\n"
            f"{pnl_line} | {status_line}"
        )
    await chat_target.answer("\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=open_keyboard)
    if is_callback:
        await target.answer()


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

    # Статистика по закрытым
    all_closed = (await session.execute(select(PaperPosition).where(PaperPosition.account_id == account.id, PaperPosition.status == PositionStatus.CLOSED.value))).scalars().all()
    total_closed = len(all_closed)
    if total_closed == 0:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Активные сделки", callback_data="nav:transactions:0", icon_custom_emoji_id=CHART_ID)],
                [InlineKeyboardButton(text="Торговать", callback_data="nav:trade", icon_custom_emoji_id=CHART_UP_ID)],
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

    wins = [p for p in all_closed if p.realized_pnl > 0]
    losses = [p for p in all_closed if p.realized_pnl <= 0]
    win_cnt = len(wins)
    loss_cnt = len(losses)
    win_rate = (win_cnt / total_closed * 100) if total_closed else 0
    total_pnl = sum((p.realized_pnl for p in all_closed), Decimal("0"))
    plus = sum((p.realized_pnl for p in wins), Decimal("0"))
    minus = sum((p.realized_pnl for p in losses), Decimal("0"))
    best = max((p.realized_pnl for p in all_closed), default=Decimal("0"))
    worst = min((p.realized_pnl for p in all_closed), default=Decimal("0"))

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
        pag_row.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"nav:history:{max(0, offset - limit)}", icon_custom_emoji_id=PIN_ID))
    if offset + limit < total_closed:
        pag_row.append(InlineKeyboardButton(text="Ещё ▶", callback_data=f"nav:history:{offset + limit}", icon_custom_emoji_id=PIN_ID))
    if pag_row:
        kb_rows.append(pag_row)
    kb_rows.append([InlineKeyboardButton(text="Активные сделки", callback_data="nav:transactions:0", icon_custom_emoji_id=GREEN_ID)])
    kb_rows.append([InlineKeyboardButton(text="Топ 10", callback_data="nav:top", icon_custom_emoji_id=GOLD_ID)])

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
            f"{p.symbol} {side_tag} {side_str} x{p.leverage or 1:g} {pnl_emoji} {fmt_money(pnl)}\n"
            f"Вход: {fmt_price(p.entry_price)} → Выход: {fmt_price(p.current_price)}  {fmt_pct((pnl / (p.notional / (p.leverage or 1)) * 100) if p.notional else 0)}\n"
            f"{p.closed_at.strftime('%d.%m %H:%M') if p.closed_at else ''}"
        )
    await chat_target.answer("\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    if is_callback:
        await target.answer()


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
