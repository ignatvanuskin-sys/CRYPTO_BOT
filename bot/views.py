from __future__ import annotations

from decimal import Decimal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emojis import (
    BOOKMARK_ID,
    CHART_UP_ID,
    CROWN_ID,
    PIN_ID,
)
from services.bingx_market_data import get_shared_snapshot


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Торговать", icon_custom_emoji_id=CHART_UP_ID)],
            [KeyboardButton(text="Личный кабинет", icon_custom_emoji_id=CROWN_ID)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def back_keyboard(target: str = "nav:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data=target, icon_custom_emoji_id=PIN_ID)]
        ]
    )


def fmt_money(value: Decimal | int | float | None) -> str:
    if value is None:
        return "—"
    return f"${Decimal(str(value)):,.2f}"


def fmt_pct(value: Decimal | int | float | None) -> str:
    if value is None:
        return "—"
    return f"{Decimal(str(value)):+.2f}%"


def bingx_chart_url(symbol: str) -> str:
    """BingX perpetual chart for the pair, e.g. SOLUSDT -> SOL-USDT."""
    base = symbol.upper().removesuffix("USDT")
    return f"https://bingx.com/en/perpetual/{base}-USDT"


async def get_display_snapshot(session: AsyncSession, symbol: str):
    """Read only the shared PostgreSQL market snapshot for user display."""
    from config import settings

    try:
        return await get_shared_snapshot(session, symbol, settings.market_data_max_age_ms)
    except Exception:
        return None
