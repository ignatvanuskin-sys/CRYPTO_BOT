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

# Форматтеры живут в нейтральном services/formatting.py — им пользуются и хендлеры,
# и пуши из services/. Реэкспорт нужен, чтобы `from bot.views import fmt_money`
# по-прежнему работал во всех экранах.
from services.formatting import (  # noqa: F401
    fmt_leverage,
    fmt_leverage_move_pct,
    fmt_money,
    fmt_pct,
    fmt_price,
    fmt_signed_money,
    format_side,
)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Торговать", icon_custom_emoji_id=CHART_UP_ID), KeyboardButton(text="Личный кабинет", icon_custom_emoji_id=CROWN_ID)],
            [KeyboardButton(text="Топ 10", icon_custom_emoji_id=GOLD_ID), KeyboardButton(text="Сделки", icon_custom_emoji_id=CHART_ID)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def back_keyboard(target: str = "nav:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Назад", target, icon=PIN_ID)]
        ]
    )


def btn(text: str, callback_data: str, icon: str | None = None, style: str | None = None) -> InlineKeyboardButton:
    """Inline-кнопка с premium-иконкой и цветом (Bot API 9.4: danger/success/primary).

    style:
      - "danger"  — красный (закрытие, отмена)
      - "success" — зелёный (подтвердить, обновить)
      - "primary" — синий (инфо, навигация)
      - None      — стандартная
    """
    kwargs: dict = {"text": text, "callback_data": callback_data}
    if icon:
        kwargs["icon_custom_emoji_id"] = icon
    if style:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def safe_edit(target_message, text: str, markup=None, parse_mode=None):
    """edit_text с fallback на answer (если сообщение изменить нельзя).

    Используется для кнопок «Обновить» — обновляет окно на месте,
    не создавая новое сообщение.
    """
    from aiogram.exceptions import TelegramBadRequest

    async def _run():
        if target_message is None:
            return
        try:
            await target_message.edit_text(text, parse_mode=parse_mode, reply_markup=markup)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return  # контент не изменился — это успех для «Обновить»
            try:
                await target_message.answer(text, parse_mode=parse_mode, reply_markup=markup)
            except Exception:
                pass
        except Exception:
            try:
                await target_message.answer(text, parse_mode=parse_mode, reply_markup=markup)
            except Exception:
                pass
    return _run()


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


def fmt_leverage(value: Decimal | int | float | None) -> str:
    """Плечо в едином виде: 'x50', 'x2.5'. Один формат во всех экранах."""
    if value is None:
        return "x1"
    try:
        d = Decimal(str(value)).normalize()
    except Exception:
        return "x1"
    if d == d.to_integral_value():
        d = d.quantize(Decimal(1))
    return f"x{d:f}"


def fmt_leverage_move_pct(value: Decimal | int | float | None) -> str:
    """Процент движения цены без знака: '0.3%', '1.8%'."""
    if value is None:
        return "—"
    d = Decimal(str(value)).normalize()
    if d == d.to_integral_value():
        d = d.quantize(Decimal(1))
    return f"{d:f}%"


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
