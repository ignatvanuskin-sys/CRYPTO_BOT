"""Цена ликвидации в UI считается по той же формуле, что закрывает позицию в движке.

Движок закрывает при `unrealized_pnl <= -margin * 1.0` (100% маржи, как на реальной бирже).
Тесты проверяют совпадение показанной цены с точкой срабатывания.
"""

from decimal import Decimal

import pytest

from bot.views import fmt_leverage, fmt_leverage_move_pct
from services.pnl import (
    LIQUIDATION_MARGIN_FRACTION,
    calc_liquidation_price,
    calc_margin,
    calc_unrealized,
    is_liquidated,
    liquidation_move_pct,
    liquidation_threshold_pnl,
)


def test_engine_uses_full_margin():
    assert LIQUIDATION_MARGIN_FRACTION == Decimal("1.0")
    # 100 маржи, плечо 10 → ликвидация при -100 (100% потерь, как на бирже)
    assert liquidation_threshold_pnl(Decimal("1000"), 10) == Decimal("-100.00")


@pytest.mark.parametrize(
    "side,entry,leverage,expected",
    [
        # L=1 → цена ликвидации = 0 (вся маржа = весь объём) → None на реальной бирже
        # Для L=1 ликвидация означает полную потерю — это x1 без маржи
        ("LONG", "100", 10, "90"),
        ("SHORT", "100", 10, "110"),
        ("LONG", "100", 50, "98"),
        ("SHORT", "100", 50, "102"),
        # 300x — 0.333% движения убивает позицию
        ("LONG", "60000", 300, "59800"),
        ("SHORT", "60000", 300, "60200"),
    ],
)
def test_estimated_price_before_open(side, entry, leverage, expected):
    assert calc_liquidation_price(side, Decimal(entry), leverage) == Decimal(expected)


def test_exact_price_uses_stored_quantity_and_notional():
    # 10 монет по $100 = notional 1000, плечо 50 → маржа 20, порог -20 → -2.0 на монету
    price = calc_liquidation_price("LONG", Decimal("100"), 50, Decimal("10"), Decimal("1000"))
    assert price == Decimal("98")
    assert calc_margin(Decimal("1000"), 50) == Decimal("20.00")


def test_enum_like_side_is_accepted():
    long_price = calc_liquidation_price("PositionSide.LONG", Decimal("100"), 10)
    short_price = calc_liquidation_price("PositionSide.SHORT", Decimal("100"), 10)
    assert long_price == Decimal("90")
    assert short_price == Decimal("110")


@pytest.mark.parametrize(
    "side,leverage,entry,qty",
    [
        ("LONG", 50, "100", "10"),
        ("SHORT", 50, "100", "10"),
        ("LONG", 300, "60000", "0.02"),
        ("SHORT", 300, "60000", "0.02"),
        ("LONG", 2, "1.2345", "500"),
        ("SHORT", 125, "0.00004321", "50000000"),
    ],
)
def test_shown_price_is_where_the_engine_closes(side, leverage, entry, qty):
    entry_d = Decimal(entry)
    qty_d = Decimal(qty)
    notional = (entry_d * qty_d).quantize(Decimal("0.01"))
    threshold = liquidation_threshold_pnl(notional, leverage)

    liq = calc_liquidation_price(side, entry_d, leverage, qty_d, notional)
    assert liq is not None and liq > 0

    # На показанной цене PnL совпадает с порогом движка (в пределах цента округления)
    at_liq = calc_unrealized(side, entry_d, liq, qty_d)
    assert abs(at_liq - threshold) <= Decimal("0.01")

    step = abs(entry_d - liq) / Decimal("100")
    worse = liq - step if side == "LONG" else liq + step
    better = liq + step if side == "LONG" else liq - step

    assert is_liquidated(calc_unrealized(side, entry_d, worse, qty_d), notional, leverage)
    assert not is_liquidated(calc_unrealized(side, entry_d, better, qty_d), notional, leverage)


@pytest.mark.parametrize(
    "leverage,expected",
    [(1, "100.00"), (10, "10.00"), (50, "2.00"), (125, "0.80"), (300, "0.33"), (None, "100.00")],
)
def test_move_pct_against_position(leverage, expected):
    assert liquidation_move_pct(leverage) == Decimal(expected)


def test_move_pct_is_displayed_without_trailing_zeros():
    assert fmt_leverage_move_pct(liquidation_move_pct(300)) == "0.33%"
    assert fmt_leverage_move_pct(liquidation_move_pct(50)) == "2%"
    assert fmt_leverage_move_pct(liquidation_move_pct(1)) == "100%"
    assert fmt_leverage_move_pct(None) == "—"


def test_leverage_has_one_format_everywhere():
    assert fmt_leverage(Decimal("50.00")) == "x50"
    assert fmt_leverage(300) == "x300"
    assert fmt_leverage(Decimal("2.50")) == "x2.5"
    assert fmt_leverage(None) == "x1"


@pytest.mark.parametrize(
    "side,entry,leverage",
    [
        ("LONG", "0", 10),          # нет цены входа
        ("LONG", "-100", 10),       # мусор в данных
        ("LONG", "100", 0),         # плечо 0
        ("LONG", "100", "не число"),
    ],
)
def test_no_price_instead_of_a_wrong_price(side, entry, leverage):
    assert calc_liquidation_price(side, entry, leverage) is None


def test_move_pct_guards():
    assert liquidation_move_pct(0) is None
    assert liquidation_move_pct("не число") is None


def test_liquidation_example_from_docstring():
    """Документированный пример: 300x, notional 3000, margin 10, crash до 1.
    L=300: liq_price = entry * (1 - 1.0/300) = entry * 0.99666...
    При entry=100: liq = 99.667 → движение 0.333% убивает."""
    liq = calc_liquidation_price("LONG", Decimal("100"), 300)
    # move = 100 * 1.0 / 300 = 0.333...
    assert liq == Decimal("99.666666666667") or abs(liq - Decimal("99.667")) < Decimal("0.01")