from __future__ import annotations

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
@router.message(F.text == "Личный кабинет")
async def cmd_profile(message: Message, session):
    if message.from_user is None:
        return
    _trade_state.pop(message.from_user.id, None)
    await _send_profile(message.from_user.id, session, message)


@router.message(Command("transactions"))
@router.message(F.text == "Сделки")
async def cmd_transactions(message: Message, session):
    if message.from_user is None:
        return
    _trade_state.pop(message.from_user.id, None)
    await _send_transactions(message.from_user.id, session, message)


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

    text = (
        f"{TG_CROWN} <b>ЛИЧНЫЙ КАБИНЕТ</b>\n\n"
        f"{tg_emoji(PIN_ID, '📌')} Юзернейм: {user.username or user.telegram_id}\n"
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
                [InlineKeyboardButton(text="Торговать", callback_data="nav:trade", icon_custom_emoji_id=CHART_UP_ID)],
            ]
        ),
    )
    if is_callback:
        await target.answer()


async def _send_transactions(telegram_id: int, session, target: Message | CallbackQuery):
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
    positions = (
        await session.execute(
            select(PaperPosition)
            .where(PaperPosition.account_id == account.id)
            .order_by(PaperPosition.opened_at.desc())
            .limit(15)
        )
    ).scalars().all()
    if not positions:
        await chat_target.answer(
            f"{TG_CHART} <b>МОИ СДЕЛКИ</b>\n\nСделок пока нет.\n\nНажмите Торговать, чтобы открыть первую.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )
        if is_callback:
            await target.answer()
        return

    open_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            *[
                [
                    InlineKeyboardButton(
                        text=f"Закрыть {p.symbol} {format_side(p.side)}",
                        callback_data=f"close_preview:{p.id}",
                        icon_custom_emoji_id=RED_ID,
                    )
                ]
                for p in positions
                if p.status == PositionStatus.OPEN.value
            ],
            [InlineKeyboardButton(text="Торговать", callback_data="nav:trade", icon_custom_emoji_id=CHART_UP_ID)],
        ]
    )
    lines = [f"{TG_CHART} <b>МОИ СДЕЛКИ</b>\n"]
    for p in positions:
        side_str = format_side(p.side)
        side_tag = TG_LONG if side_str == "LONG" else TG_SHORT
        if p.status == PositionStatus.OPEN.value:
            pnl_line = f"PnL: {fmt_money(p.unrealized_pnl)}"
            status_line = f"{tg_emoji(GREEN_ID, '🟢')} ОТКРЫТА"
        else:
            pnl_line = f"PnL: {fmt_money(p.realized_pnl)}"
            status_line = f"{tg_emoji(CROSS_ID, '❌')} ЗАКРЫТА"
        lines.append(
            f"{p.symbol} {side_tag} {side_str} x{p.leverage or 1:g}\n"
            f"Вход: {fmt_price(p.entry_price)} → Выход: {fmt_price(p.current_price)}\n"
            f"{pnl_line} | {status_line}"
        )
    await chat_target.answer("\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=open_keyboard)
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


@router.callback_query(F.data == "nav:profile")
async def nav_profile(callback: CallbackQuery, session):
    if callback.from_user is None:
        await callback.answer()
        return
    _trade_state.pop(callback.from_user.id, None)
    await _send_profile(callback.from_user.id, session, callback)


@router.callback_query(F.data == "nav:transactions")
async def nav_transactions(callback: CallbackQuery, session):
    if callback.from_user is None:
        await callback.answer()
        return
    _trade_state.pop(callback.from_user.id, None)
    await _send_transactions(callback.from_user.id, session, callback)
