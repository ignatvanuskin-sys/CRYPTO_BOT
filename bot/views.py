from __future__ import annotations

from decimal import Decimal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emojis import (
    BOOKMARK_ID,
    CHART_ID,
    CHART_UP_ID,
    CROWN_ID,
    GOLD_ID,
    PIN_ID,
)
from services.bingx_market_data import get_shared_snapshot


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Торговать", icon_custom_emoji_id=CHART_UP_ID), KeyboardButton(text="Личный кабинет", icon_custom_emoji_id=CROWN_ID)],
            [KeyboardButton(text="Топ 10", icon_custom_emoji_id=GOLD_ID), KeyboardButton(text="Позиции", icon_custom_emoji_id=CHART_ID)],
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


def fmt_price(value: Decimal | int | float | None, precision: int | None = None) -> str:
    if value is None:
        return "—"
    d = Decimal(str(value))
    if precision is not None:
        quant = Decimal(10) ** -int(precision)
        try:
            return f"${d.quantize(quant):,f}"
        except Exception:
            pass
    # Auto precision based on magnitude — makes low-price coins readable
    abs_d = abs(d)
    if abs_d == 0:
        return "$0.00"
    if abs_d >= 1000:
        quant = Decimal("0.01")
    elif abs_d >= 1:
        quant = Decimal("0.0001")
    elif abs_d >= 0.1:
        quant = Decimal("0.000001")
    else:
        quant = Decimal("0.00000001")
    try:
        return f"${d.quantize(quant):,f}"
    except Exception:
        return f"${d:,.8f}".rstrip("0").rstrip(".")


def format_side(side) -> str:
    """Enum or string → 'LONG'/'SHORT'."""
    if side is None:
        return "—"
    if hasattr(side, "value"):
        return str(side.value)
    txt = str(side)
    if "." in txt:
        return txt.split(".")[-1]
    return txt


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
