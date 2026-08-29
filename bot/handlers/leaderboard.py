from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import html

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from bot.emojis import (
    BRONZE_ID,
    CHART_ID,
    CHART_UP_ID,
    CROWN_ID,
    DIAMOND_ID,
    GOLD_ID,
    GREEN_ID,
    PIN_ID,
    RED_ID,
    SILVER_ID,
    STAR_ID,
    TG_LONG,
    TG_SHORT,
    tg_emoji,
)
from bot.views import btn, fmt_money, fmt_pct, main_menu
from db.competition_models import Competition, CompetitionStatus, LeaderboardSnapshot
from db.models import User
from services.competition import get_active_competition
from services.leaderboard import build_leaderboard, get_top_n, get_user_rank

router = Router()

TG_GOLD = tg_emoji(GOLD_ID, "🥇")
TG_SILVER = tg_emoji(SILVER_ID, "🥈")
TG_BRONZE = tg_emoji(BRONZE_ID, "🥉")
TG_CROWN = tg_emoji(CROWN_ID, "👑")
TG_STAR = tg_emoji(STAR_ID, "⭐️")
TG_CHART = tg_emoji(CHART_ID, "📊")
TG_DIAMOND = tg_emoji(DIAMOND_ID, "💎")
TG_PIN = tg_emoji(PIN_ID, "📌")


def _medal(rank: int) -> str:
    if rank == 1:
        return TG_GOLD
    if rank == 2:
        return TG_SILVER
    if rank == 3:
        return TG_BRONZE
    return f"{rank}."


def _format_leaderboard_text(title: str, leaderboard: list[dict], users_map: dict[int, User], is_final: bool, offset: int = 0) -> str:
    """Красивая таблица топ-10 с пагинацией."""
    total = len(leaderboard)
    # For display, slice by offset
    page = leaderboard[offset:offset+10]
    header = f"{TG_CROWN} <b>{title}</b>\n"
    if is_final:
        header += f"{TG_STAR} <i>Итоги недели — финальный топ-{10 if total>=10 else total}</i>\n"
    else:
        header += f"{TG_CHART} <i>Live топ — обновляется каждую сделку</i>\n"
    header += f"Страница {offset//10+1}/{(total+9)//10} — всего {total}\n"
    header += "━━━━━━━━━━━━━━━━━━━━\n\n"

    if not page:
        return header + "Пока нет участников. Открой первую сделку!"

    lines = []
    for entry in page:
        rank = entry["rank"]
        user = users_map.get(entry["user_id"])
        raw_name = user.username if user and user.username else f"ID{entry['user_id']}"
        name = html.escape(raw_name[:16])
        medal = _medal(rank)
        roi = fmt_pct(entry["roi"])
        equity = fmt_money(entry["equity"])
        # Make top3 bold
        if rank <= 3:
            lines.append(f"{medal} <b>{name}</b>\n   {roi}  {equity}")
        else:
            lines.append(f"{medal} {name}\n   {roi}  {equity}")

    body = "\n\n".join(lines)
    return header + body


async def _get_leaderboard_for_display(session, offset: int = 0):
    """Возвращает (title, full_leaderboard, users_map, is_final, competition, total).

    Возвращает ПОЛНЫЙ список — форматтер сам режет страницу по offset,
    а футер «Твоё место» всегда находит юзера независимо от страницы.
    """
    comp = await get_active_competition(session)
    if comp is not None:
        lb = await build_leaderboard(session, comp.id)
        total = len(lb)
        user_ids = [e["user_id"] for e in lb[offset:offset+10]]
        users_map = {}
        if user_ids:
            res = await session.execute(select(User).where(User.id.in_(user_ids)))
            for u in res.scalars().all():
                users_map[u.id] = u
        title = comp.name.upper()
        return title, lb, users_map, False, comp, total

    # No active — show last finished
    res = await session.execute(
        select(Competition).where(Competition.status == CompetitionStatus.FINISHED.value).order_by(Competition.ends_at.desc()).limit(1)
    )
    comp = res.scalar_one_or_none()
    if comp is not None:
        from sqlalchemy import func as sa_func

        total = (await session.execute(select(sa_func.count()).select_from(LeaderboardSnapshot).where(LeaderboardSnapshot.competition_id == comp.id))).scalar_one()
        snap_res = await session.execute(
            select(LeaderboardSnapshot).where(LeaderboardSnapshot.competition_id == comp.id).order_by(LeaderboardSnapshot.rank)
        )
        snaps = snap_res.scalars().all()
        if snaps:
            lb = []
            for s in snaps:
                lb.append({"rank": s.rank, "user_id": s.user_id, "roi": s.roi, "equity": s.equity})
            user_ids = [e["user_id"] for e in lb[offset:offset+10]]
            users_map = {}
            if user_ids:
                res2 = await session.execute(select(User).where(User.id.in_(user_ids)))
                for u in res2.scalars().all():
                    users_map[u.id] = u
            title = f"{comp.name.upper()} — ФИНАЛ"
            return title, lb, users_map, True, comp, total
        lb = await build_leaderboard(session, comp.id)
        total = len(lb)
        user_ids = [e["user_id"] for e in lb[offset:offset+10]]
        users_map = {}
        if user_ids:
            res = await session.execute(select(User).where(User.id.in_(user_ids)))
            for u in res.scalars().all():
                users_map[u.id] = u
        title = f"{comp.name.upper()} — ФИНАЛ"
        return title, lb, users_map, True, comp, total

    return None, [], {}, False, None, 0


@router.message(Command("top", ignore_case=True))
@router.message(Command("топ", ignore_case=True))
@router.message(Command("leaderboard", ignore_case=True))
@router.message(Command("leaders", ignore_case=True))
@router.message(Command("лидеры", ignore_case=True))
@router.message(Command("таблица_лидеров", ignore_case=True))
@router.message(F.text.in_({"Топ", "Топ 10", "Лидеры", "Таблица лидеров", "🏆 Топ 10", "🏆 Топ"}))
async def cmd_top(message: Message, session):
    if message.from_user is None:
        return
    # Clear trade_state if any
    try:
        from bot.handlers.trade import trade_state
        trade_state.pop(message.from_user.id, None)
    except Exception:
        pass

    title, lb, users_map, is_final, comp, total = await _get_leaderboard_for_display(session, offset=0)

    if comp is None:
        await message.answer(
            f"{TG_CHART} <b>Топ пока пуст</b>\n\nТурнир ещё не начался. Открой сделку — попадёшь в таблицу!",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )
        return

    text = _format_leaderboard_text(title, lb, users_map, is_final, offset=0)
    # Add user's own rank footer — lb is the FULL list, user is always findable
    user = (await session.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one_or_none()
    if user and not is_final:
        entry = next((e for e in lb if e["user_id"] == user.id), None)
        if entry is None:
            text += f"\n\n━━━━━━━━━━━━━━━━━━━━\n{TG_PIN} Ты пока не участвуешь — открой сделку!"
        elif entry["rank"] <= 10:
            text += f"\n\n━━━━━━━━━━━━━━━━━━━━\n{TG_PIN} Ты в топ-10! <b>#{entry['rank']}</b>  {fmt_pct(entry['roi'])}"
        else:
            text += f"\n\n━━━━━━━━━━━━━━━━━━━━\n{TG_PIN} Твоё место: <b>#{entry['rank']}</b>  {fmt_pct(entry['roi'])}"
    elif user and is_final:
        snap = (await session.execute(select(LeaderboardSnapshot).where(LeaderboardSnapshot.competition_id == comp.id, LeaderboardSnapshot.user_id == user.id))).scalar_one_or_none()
        if snap:
            text += f"\n\n━━━━━━━━━━━━━━━━━━━━\n{TG_PIN} Твоё место: <b>#{snap.rank}</b>  {fmt_pct(snap.roi)}"
        else:
            text += f"\n\n━━━━━━━━━━━━━━━━━━━━\n{TG_PIN} Ты не в топ-10 этой недели"

    # Time left for active
    if comp and not is_final:
        secs = max(0, int((comp.ends_at - datetime.now(timezone.utc)).total_seconds()))
        days, rem = divmod(secs, 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        if days > 0:
            time_left = f"{days}д {hours}ч"
        else:
            time_left = f"{hours}ч {mins}м"
        text += f"\n{tg_emoji('5413879192267805083', '🗓')} До итогов: {time_left}"

    # Pagination for initial view (page 0)
    kb_rows = []
    if total > 10:
        kb_rows.append(btn("Ещё ▶", "nav:top:10", icon=PIN_ID))
    kb_rows.append(btn("Обновить", "nav:top:0", icon=CHART_ID, style="success"))
    kb_rows.append(btn("Торговать", "nav:trade", icon=CHART_UP_ID, style="success"))
    kb_rows.append(btn("Назад", "nav:home", icon=PIN_ID))
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@router.message(Command("positions", ignore_case=True))
@router.message(Command("позиции", ignore_case=True))
@router.message(Command("pozicii", ignore_case=True))
@router.message(Command("открытые", ignore_case=True))
@router.message(F.text.in_({"Позиции", "Открытые позиции", "Мои позиции"}))
async def cmd_positions(message: Message, session):
    if message.from_user is None:
        return
    try:
        from bot.handlers.trade import trade_state
        trade_state.pop(message.from_user.id, None)
    except Exception:
        pass
    content = await _build_open_positions(message.from_user.id, session)
    if content is None:
        await message.answer("Сначала отправь /start", reply_markup=main_menu())
        return
    text, markup = content
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=markup)


@router.callback_query(F.data == "nav:positions")
async def nav_positions_refresh(callback: CallbackQuery, session):
    """Обновить открытые позиции (edit_text на месте)."""
    if callback.from_user is None:
        await callback.answer()
        return
    try:
        from bot.handlers.trade import trade_state
        trade_state.pop(callback.from_user.id, None)
    except Exception:
        pass
    if callback.message:
        content = await _build_open_positions(callback.from_user.id, session)
        if content is not None:
            text, markup = content
            try:
                await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                await callback.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    await callback.answer()


async def _build_open_positions(telegram_id: int, session):
    """Возвращает (text, markup) или None если юзер/аккаунт не найден."""
    from db.paper_models import PaperPosition, PositionStatus, TradingAccount
    from bot.views import fmt_money, fmt_price, format_side
    from bot.emojis import TG_LONG, TG_SHORT

    user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    if not user:
        return None
    account = (await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    if not account:
        return None
    positions = (
        await session.execute(
            select(PaperPosition).where(PaperPosition.account_id == account.id, PaperPosition.status == PositionStatus.OPEN.value).order_by(PaperPosition.opened_at.desc())
        )
    ).scalars().all()
    if not positions:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn("Посмотреть все сделки", "nav:history:0", icon=CHART_ID, style="primary")],
        ])
        return (f"{TG_CHART} <b>ОТКРЫТЫЕ ПОЗИЦИИ</b>\n\nОткрытых позиций нет.\nНажми Торговать, чтобы открыть.", kb)

    lines = [f"{TG_CHART} <b>ОТКРЫТЫЕ ПОЗИЦИИ</b> — {len(positions)}\n"]
    kb_rows = []
    for p in positions:
        side_str = format_side(p.side)
        side_tag = TG_LONG if side_str == "LONG" else TG_SHORT
        pnl = p.unrealized_pnl
        pnl_str = fmt_money(pnl)
        pnl_emoji = tg_emoji(GREEN_ID, "🟢") if pnl > 0 else tg_emoji(RED_ID, "🔴") if pnl < 0 else "⚪️"
        lines.append(
            f"{side_tag} <b>{p.symbol} {side_str} x{int(p.leverage)} </b> {pnl_emoji} {pnl_str}\n"
            f"Вход {fmt_price(p.entry_price)} → Сейчас {fmt_price(p.current_price)}\n"
            f"Объём {fmt_money(p.notional)}"
        )
        kb_rows.append(btn(f"Закрыть {p.symbol}", f"close_preview:{p.id}", icon=RED_ID, style="danger"))
    kb_rows.append(btn("Обновить", "nav:positions", icon=GREEN_ID, style="success"))
    kb_rows.append(btn("Сделки (все)", "nav:history:0", icon=CHART_ID, style="primary"))
    kb_rows.append(btn("Топ", "nav:top", icon=GOLD_ID, style="primary"))
    return ("\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data.startswith("nav:top"))
async def nav_top(callback: CallbackQuery, session):
    # Reuse cmd_top logic but for callback, supports pagination nav:top:10 etc.
    if callback.from_user is None:
        await callback.answer()
        return
    try:
        from bot.handlers.trade import trade_state
        trade_state.pop(callback.from_user.id, None)
    except Exception:
        pass
    # Parse offset
    offset = 0
    parts = callback.data.split(":")
    if len(parts) == 3:
        try:
            offset = int(parts[2])
        except ValueError:
            offset = 0
    title, lb, users_map, is_final, comp, total = await _get_leaderboard_for_display(session, offset=offset)
    if comp is None:
        if callback.message:
            await callback.message.answer(
                f"{TG_CHART} <b>Топ пока пуст</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu(),
            )
        await callback.answer()
        return
    text = _format_leaderboard_text(title, lb, users_map, is_final, offset=offset)
    # Add user rank footer — lb is the FULL list, no second build needed
    user = (await session.execute(select(User).where(User.telegram_id == callback.from_user.id))).scalar_one_or_none()
    if user and comp:
        if is_final:
            snap = (await session.execute(select(LeaderboardSnapshot).where(LeaderboardSnapshot.competition_id == comp.id, LeaderboardSnapshot.user_id == user.id))).scalar_one_or_none()
            if snap:
                text += f"\n\n━━━━━━━━━━━━━━━━━━━━\n{TG_PIN} Твоё место: <b>#{snap.rank}</b>  {fmt_pct(snap.roi)}"
            else:
                text += f"\n\n━━━━━━━━━━━━━━━━━━━━\n{TG_PIN} Ты не в топ-10"
        else:
            entry = next((e for e in lb if e["user_id"] == user.id), None)
            if entry is None:
                text += f"\n\n━━━━━━━━━━━━━━━━━━━━\n{TG_PIN} Ты пока не участвуешь — открой сделку!"
            elif entry["rank"] <= 10:
                text += f"\n\n━━━━━━━━━━━━━━━━━━━━\n{TG_PIN} Ты в топ-10! <b>#{entry['rank']}</b>"
            else:
                text += f"\n\n━━━━━━━━━━━━━━━━━━━━\n{TG_PIN} Твоё место: <b>#{entry['rank']}</b>"

    if comp and not is_final:
        secs = max(0, int((comp.ends_at - datetime.now(timezone.utc)).total_seconds()))
        days, rem = divmod(secs, 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        time_left = f"{days}д {hours}ч" if days > 0 else f"{hours}ч {mins}м"
        text += f"\n{tg_emoji('5413879192267805083', '🗓')} До итогов: {time_left}"

    # Pagination keyboard
    kb_rows = []
    pag = []
    if offset > 0:
        pag.append(btn("◀ Назад", f"nav:top:{max(0, offset-10)}", icon=PIN_ID))
    if offset + 10 < total:
        pag.append(btn("Ещё ▶", f"nav:top:{offset+10}", icon=PIN_ID))
    if pag:
        kb_rows.append(pag)
    kb_rows.append(btn("Обновить", f"nav:top:{offset}", icon=CHART_ID, style="success"))
    kb_rows.append(btn("Торговать", "nav:trade", icon=CHART_UP_ID, style="success"))
    kb_rows.append(btn("Назад", "nav:home", icon=PIN_ID))
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            await callback.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    await callback.answer()
