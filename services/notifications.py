from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncEngine

from config import settings
from services.formatting import (
    PLAY_ID,
    TG_CHECK,
    TG_LONG,
    TG_RED,
    TG_SHORT,
    TG_SIREN,
    fmt_leverage,
    fmt_signed_money,
)
from db.competition_models import Competition, CompetitionPrize, LeaderboardSnapshot
from db.models import User

logger = logging.getLogger(__name__)


_CLOSE_TITLES = {
    "TP": (TG_CHECK, "ТЕЙК-ПРОФИТ СРАБОТАЛ"),
    "SL": (TG_RED, "СТОП-ЛОСС СРАБОТАЛ"),
    "LIQUIDATION": (TG_SIREN, "ЛИКВИДАЦИЯ"),
}

_CLOSE_REASONS = {
    "TP": "тейк-профит",
    "SL": "стоп-лосс",
    "LIQUIDATION": "ликвидация",
}

# Клавиатура на десяток позиций нечитаема — в сводке предлагаем повтор
# только по первым закрытым позициям.
MAX_RETRY_BUTTONS = 3


def _side_tag(side) -> str:
    return TG_LONG if str(side or "").upper().endswith("LONG") else TG_SHORT


def _close_notification_text(event: dict) -> str:
    reason = str(event.get("reason") or "")
    icon, title = _CLOSE_TITLES.get(reason, (TG_CHECK, "ПОЗИЦИЯ ЗАКРЫТА"))
    side = str(event.get("side") or "")
    pnl = fmt_signed_money(event.get("pnl"))
    head = (
        f"{icon} <b>{title}</b>\n\n"
        f"{event.get('symbol')} {_side_tag(side)} {side} {fmt_leverage(event.get('leverage'))}\n"
    )
    if reason == "LIQUIDATION":
        return (
            head
            + f"Позиция закрыта принудительно: <b>{pnl}</b>\n\n"
            + "Убыток дошёл до 90% маржи — дальше держать было нечем."
        )
    return head + f"Позиция закрыта: <b>{pnl}</b>"


def _batch_notification_text(events: list[dict]) -> str:
    """Сводка, когда за один цикл движка у юзера закрылось несколько позиций.

    Резкое движение рынка ликвидирует пачку позиций сразу — N отдельных пушей
    подряд читаются как спам, поэтому отправляем одно сообщение со списком.
    """
    has_liquidation = any(str(event.get("reason") or "") == "LIQUIDATION" for event in events)
    icon = TG_SIREN if has_liquidation else TG_CHECK
    total = sum(Decimal(str(event.get("pnl") or 0)) for event in events)
    lines = "\n".join(
        f"{event.get('symbol')} {_side_tag(event.get('side'))} {event.get('side')}"
        f" {fmt_leverage(event.get('leverage'))}"
        f" — {_CLOSE_REASONS.get(str(event.get('reason') or ''), 'закрыта')}:"
        f" <b>{fmt_signed_money(event.get('pnl'))}</b>"
        for event in events
    )
    return (
        f"{icon} <b>ЗАКРЫТО ПОЗИЦИЙ: {len(events)}</b>\n\n"
        f"{lines}\n\n"
        f"Итог: <b>{fmt_signed_money(total)}</b>"
    )


def _retry_button(text: str, position_id: int) -> InlineKeyboardButton:
    kwargs: dict = {
        "text": text,
        "callback_data": f"retry:{position_id}",
        "icon_custom_emoji_id": PLAY_ID,
        "style": "primary",
    }
    return InlineKeyboardButton(**kwargs)


def _retry_keyboard(events: list[dict]) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    single = len(events) == 1
    for event in events[:MAX_RETRY_BUTTONS]:
        position_id = event.get("position_id")
        if not position_id:
            continue
        label = "Повторить сделку" if single else f"Повторить {event.get('symbol')}"
        rows.append([_retry_button(label, int(position_id))])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def group_close_events_by_user(events: list[dict]) -> dict[int, list[dict]]:
    """События одного цикла движка → {user_id: [события]}, порядок закрытий сохранён."""
    grouped: dict[int, list[dict]] = {}
    for event in events:
        user_id = event.get("user_id")
        if not user_id:
            continue
        grouped.setdefault(int(user_id), []).append(event)
    return grouped


async def notify_positions_closed(engine: AsyncEngine, events: list[dict]) -> None:
    """Best-effort пуши об автоматических закрытиях (TP / SL / ликвидация).

    Вызывается только после коммита финансовой транзакции. Любая ошибка
    отправки логируется и не влияет на состояние счёта.
    """
    if not events:
        return
    if not settings.bot_token:
        logger.warning("Close notifications skipped: BOT_TOKEN is not configured")
        return
    grouped = group_close_events_by_user(events)
    if not grouped:
        return
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        rows = await session.execute(select(User).where(User.id.in_(grouped.keys())))
        users = {user.id: user for user in rows.scalars().all()}
    bot = Bot(token=settings.bot_token)
    try:
        # Один пуш на пользователя за цикл: пачка ликвидаций на резком движении
        # рынка не должна превращаться в очередь отдельных сообщений.
        for user_id, user_events in grouped.items():
            user = users.get(user_id)
            if not user or user.is_simulated or user.telegram_id <= 0:
                continue
            text = (
                _close_notification_text(user_events[0])
                if len(user_events) == 1
                else _batch_notification_text(user_events)
            )
            try:
                await bot.send_message(
                    user.telegram_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=_retry_keyboard(user_events),
                )
            except Exception:
                # Notification errors must never affect financial state.
                logger.exception(
                    "Close notification failed for user %s (%s positions)",
                    user_id,
                    len(user_events),
                )
    finally:
        await bot.session.close()


async def notify_competition_finished(engine: AsyncEngine, competition_id: int) -> None:
    """Best-effort result notifications after financial finalization commits."""
    if not settings.bot_token:
        logger.warning("Competition notification skipped: BOT_TOKEN is not configured")
        return
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        competition = await session.get(Competition, competition_id)
        if not competition:
            return
        rows = await session.execute(
            select(LeaderboardSnapshot, User)
            .join(User, User.id == LeaderboardSnapshot.user_id)
            .where(LeaderboardSnapshot.competition_id == competition_id)
            .order_by(LeaderboardSnapshot.rank)
        )
        prizes = await session.execute(
            select(CompetitionPrize).where(CompetitionPrize.competition_id == competition_id)
        )
        prize_by_rank = {prize.rank: prize.amount for prize in prizes.scalars().all()}
        bot = Bot(token=settings.bot_token)
        try:
            for snapshot, user in rows.all():
                if user.is_simulated or user.telegram_id <= 0:
                    continue
                prize = prize_by_rank.get(snapshot.rank)
                prize_line = f"🎁 Приз: ${Decimal(str(prize)):.2f}" if prize is not None else "В TOP 10 не вошёл."
                text = (
                    f"🏁 ТУРНИР ЗАВЕРШЁН\n\n{competition.name}\n\n"
                    f"Твой результат: #{snapshot.rank}\n"
                    f"📈 ROI: {snapshot.roi:+.2f}%\n"
                    f"💰 Equity: ${snapshot.equity:,.2f}\n"
                    f"{prize_line}\n\nСледующий турнир уже скоро 🚀"
                )
                try:
                    await bot.send_message(user.telegram_id, text)
                except Exception:
                    # Notification errors must never affect financial state.
                    logger.exception("Competition notification failed for user %s", user.id)
        finally:
            await bot.session.close()
