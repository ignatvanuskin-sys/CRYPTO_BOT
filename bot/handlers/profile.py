from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from bot.views import (
    back_keyboard,
    competition_text,
    fmt_money,
    fmt_pct,
    get_display_snapshot,
    main_menu,
    start_text,
)
from db.competition_models import Competition
from db.models import User
from db.paper_models import PaperPosition, PositionStatus, TradingAccount
from services.competition import get_active_competition, get_or_create_default_competition, join_competition
from services.trading_account import get_or_create_trading_account
from services.leaderboard import get_user_rank
from services.metrics import increment
from services.pnl import calc_unrealized

router = Router()


def home_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 ТОРГОВАТЬ", callback_data="nav:trade")],
            [InlineKeyboardButton(text="🏆 TOP 10", callback_data="nav:top")],
            [
                InlineKeyboardButton(text="👤 ПРОФИЛЬ", callback_data="nav:profile"),
                InlineKeyboardButton(text="💼 ПОЗИЦИИ", callback_data="nav:positions"),
            ],
            [InlineKeyboardButton(text="🏆 О ТУРНИРЕ", callback_data="nav:competition")],
        ]
    )


async def _get_user(message: Message, session):
    return (
        await session.execute(select(User).where(User.telegram_id == message.from_user.id))
    ).scalar_one_or_none()


@router.message(Command("start"))
async def cmd_start_new(message: Message, session):
    increment("users_started")
    user = await _get_user(message, session)
    is_new = user is None
    if user is None:
        user = User(telegram_id=message.from_user.id, username=message.from_user.username)
        session.add(user)
        await session.flush()

    account = await get_or_create_trading_account(session, user.id)
    parts = (message.text or "").split(maxsplit=1)
    start_arg = parts[1] if len(parts) == 2 else ""
    competition = None
    if start_arg.startswith("competition_") or start_arg.startswith("competition="):
        raw_id = start_arg.replace("competition_", "", 1).replace("competition=", "", 1)
        if raw_id.isdigit():
            competition = await session.get(Competition, int(raw_id))
    if competition is None or competition.status != "ACTIVE" or competition.ends_at <= datetime.now(timezone.utc):
        competition = await get_or_create_default_competition(session)
    await join_competition(session, user.id, competition.id)
    await session.commit()

    text = await start_text(session, user, account, competition)
    if is_new:
        text = text.replace("🏆 CRYPTO TRADING ARENA", "🎉 ДОБРО ПОЖАЛОВАТЬ В CRYPTO TRADING ARENA", 1)
    await message.answer(text, reply_markup=main_menu())
    await message.answer("Выбери действие:", reply_markup=home_inline())


@router.message(Command("profile"))
@router.message(F.text.in_({"👤 Профиль", "📊 MY PROFILE", "💼 Личный кабинет"}))
async def cmd_profile(message: Message, session):
    increment("profile_viewed")
    user = await _get_user(message, session)
    if not user:
        await message.answer("Сначала отправь /start", reply_markup=main_menu())
        return
    account = (
        await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    ).scalar_one_or_none()
    if not account:
        await message.answer("Профиль ещё не создан. Отправь /start", reply_markup=main_menu())
        return
    competition = await get_active_competition(session)
    rank_info = await get_user_rank(session, competition.id, user.id) if competition else None
    positions = (
        await session.execute(select(PaperPosition).where(PaperPosition.account_id == account.id))
    ).scalars().all()
    closed = [position for position in positions if position.status == PositionStatus.CLOSED.value]
    wins = len([position for position in closed if position.realized_pnl > 0])
    best = max((position.realized_pnl for position in closed), default=0)
    worst = min((position.realized_pnl for position in closed), default=0)
    rank = f"#{rank_info['rank']}" if rank_info else "—"
    roi = fmt_pct(rank_info["roi"]) if rank_info else "+0.00%"
    text = (
        "👤 ТВОЙ ПРОФИЛЬ\n\n"
        f"💰 Equity: {fmt_money(account.equity)}\n"
        f"📈 ROI: {roi}\n"
        f"🏆 Rank: {rank}\n\n"
        "━━━━━━━━━━━━\n\n"
        f"📊 СДЕЛКИ\nВсего: {len(positions)}\n"
        f"Побед: {wins}\nПоражений: {len(closed) - wins}\n"
        f"Win Rate: {(wins / len(closed) * 100) if closed else 0:.1f}%\n\n"
        f"Лучший трейд: {fmt_money(best)}\n"
        f"Худший трейд: {fmt_money(worst)}"
    )
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📜 ИСТОРИЯ", callback_data="nav:history")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="nav:home")],
            ]
        ),
    )


@router.message(Command("history"))
@router.message(F.text == "📜 История")
async def cmd_history(message: Message, session):
    user = await _get_user(message, session)
    if not user:
        await message.answer("Сначала отправь /start", reply_markup=main_menu())
        return
    account = (
        await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    ).scalar_one_or_none()
    if not account:
        await message.answer("История пока пуста.", reply_markup=back_keyboard("nav:home"))
        return
    positions = (
        await session.execute(
            select(PaperPosition)
            .where(PaperPosition.account_id == account.id, PaperPosition.status == PositionStatus.CLOSED.value)
            .order_by(PaperPosition.closed_at.desc())
            .limit(10)
        )
    ).scalars().all()
    if not positions:
        await message.answer("📜 ИСТОРИЯ\n\nЗакрытых сделок пока нет.", reply_markup=back_keyboard("nav:home"))
        return
    lines = ["📜 ИСТОРИЯ\n"]
    for position in positions:
        emoji = "🟢" if position.realized_pnl >= 0 else "🔴"
        lines.append(
            f"{emoji} {position.symbol} {position.side} {fmt_money(position.realized_pnl)}\n"
            f"Вход {fmt_money(position.entry_price)} → выход {fmt_money(position.current_price)}"
        )
    await message.answer("\n\n━━━━━━━━━━━━\n\n".join(lines), reply_markup=back_keyboard("nav:home"))


@router.message(Command("positions"))
@router.message(F.text.in_({"💼 Позиции", "📈 MY POSITIONS"}))
async def cmd_positions_list(message: Message, session):
    user = await _get_user(message, session)
    if not user:
        await message.answer("Сначала отправь /start", reply_markup=main_menu())
        return
    account = (
        await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    ).scalar_one_or_none()
    if not account:
        await message.answer("Позиции появятся после /trade", reply_markup=main_menu())
        return
    positions = (
        await session.execute(
            select(PaperPosition)
            .where(PaperPosition.account_id == account.id, PaperPosition.status == PositionStatus.OPEN.value)
            .order_by(PaperPosition.opened_at.desc())
        )
    ).scalars().all()
    if not positions:
        await message.answer("💼 МОИ ПОЗИЦИИ\n\nОткрытых позиций нет.\n\nНажми 🚀 Торговать, чтобы начать.", reply_markup=back_keyboard("nav:home"))
        return
    await message.answer("💼 МОИ ПОЗИЦИИ", reply_markup=back_keyboard("nav:home"))
    for position in positions:
        snapshot = await get_display_snapshot(session, position.symbol)
        current = snapshot.bid if snapshot and position.side == "LONG" else snapshot.ask if snapshot else None
        pnl = calc_unrealized(position.side, position.entry_price, current, position.quantity) if current else None
        distance_tp = ((position.take_profit - current) / current * 100) if position.take_profit and current else None
        distance_sl = ((position.stop_loss - current) / current * 100) if position.stop_loss and current else None
        text = (
            f"📈 {position.symbol} {position.side}\n\n"
            f"Размер: {fmt_money(position.notional)}\n"
            f"Вход: {fmt_money(position.entry_price)}\n"
            f"Текущая цена: {fmt_money(current) if current else '⚠️ рынок no_data'}\n"
            f"PnL: {fmt_money(pnl) if pnl is not None else '—'}\n\n"
            f"🎯 TP: {fmt_money(position.take_profit)}"
            + (f" ({distance_tp:+.2f}%)" if distance_tp is not None else "")
            + f"\n🛑 SL: {fmt_money(position.stop_loss)}"
            + (f" ({distance_sl:+.2f}%)" if distance_sl is not None else "")
        )
        await message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔴 ЗАКРЫТЬ ПОЗИЦИЮ", callback_data=f"close_preview:{position.id}")]]
            ),
        )


@router.message(Command("competition"))
@router.message(F.text == "🏆 Турнир")
async def cmd_competition(message: Message, session):
    competition = await get_active_competition(session)
    if competition is None:
        await message.answer("⏳ Активного турнира сейчас нет.", reply_markup=main_menu())
        return
    await message.answer(await competition_text(session, competition), reply_markup=back_keyboard("nav:home"))


@router.message(Command("help"))
@router.message(F.text.in_({"ℹ️ Как играть", "📜 Правила"}))
async def cmd_help(message: Message):
    await message.answer(
        "ℹ️ КАК ИГРАТЬ\n\n"
        "1. Каждый игрок получает $10,000 демо.\n\n"
        "2. Торгуй BTC, ETH и SOL по реальным ценам BingX.\n\n"
        "3. Можно открывать LONG и SHORT.\n\n"
        "4. LONG открывается по ASK и закрывается по BID.\n"
        "5. SHORT открывается по BID и закрывается по ASK.\n\n"
        "6. PnL, ROI и рейтинг считает сервер.\n\n"
        "7. Побеждают лучшие по ROI. После завершения турнира новые сделки не принимаются.",
        reply_markup=back_keyboard("nav:home"),
    )


@router.callback_query(F.data == "nav:home")
async def nav_home(callback: CallbackQuery, session):
    await cmd_start_new(callback.message, session)
    await callback.answer()


@router.callback_query(F.data == "nav:profile")
async def nav_profile(callback: CallbackQuery, session):
    await cmd_profile(callback.message, session)
    await callback.answer()


@router.callback_query(F.data == "nav:positions")
async def nav_positions(callback: CallbackQuery, session):
    await cmd_positions_list(callback.message, session)
    await callback.answer()


@router.callback_query(F.data == "nav:history")
async def nav_history(callback: CallbackQuery, session):
    await cmd_history(callback.message, session)
    await callback.answer()


@router.callback_query(F.data == "nav:competition")
async def nav_competition(callback: CallbackQuery, session):
    await cmd_competition(callback.message, session)
    await callback.answer()


@router.callback_query(F.data == "nav:help")
async def nav_help(callback: CallbackQuery):
    await cmd_help(callback.message)
    await callback.answer()
