"""Цена ликвидации в UI считается по той же формуле, что закрывает позицию в движке.

Движок закрывает при `unrealized_pnl <= -margin * 0.9` (services.pnl), поэтому тесты
проверяют не «красивое число», а совпадение показанной цены с точкой срабатывания.
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


def test_engine_buffer_is_ninety_percent_of_margin():
    assert LIQUIDATION_MARGIN_FRACTION == Decimal("0.9")
    # 100 маржи, плечо 10 → ликвидация при -90, а не при -100
    assert liquidation_threshold_pnl(Decimal("1000"), 10) == Decimal("-90.00")


@pytest.mark.parametrize(
    "side,entry,leverage,expected",
    [
        # E × (1 ∓ 0.9 / L)
        ("LONG", "100", 1, "10"),
        ("LONG", "100", 10, "91"),
        ("SHORT", "100", 10, "109"),
        ("LONG", "100", 50, "98.2"),
        ("SHORT", "100", 50, "101.8"),
        # 300x — половина процента убивает позицию
        ("LONG", "60000", 300, "59820"),
        ("SHORT", "60000", 300, "60180"),
    ],
)
def test_estimated_price_before_open(side, entry, leverage, expected):
    assert calc_liquidation_price(side, Decimal(entry), leverage) == Decimal(expected)


def test_exact_price_uses_stored_quantity_and_notional():
    # 10 монет по $100 = notional 1000, плечо 50 → маржа 20, порог -18 → -1.8 на монету
    price = calc_liquidation_price("LONG", Decimal("100"), 50, Decimal("10"), Decimal("1000"))
    assert price == Decimal("98.2")
    assert calc_margin(Decimal("1000"), 50) == Decimal("20.00")


def test_enum_like_side_is_accepted():
    """Из БД side приходит как 'PositionSide.LONG' — направление не должно теряться."""
    long_price = calc_liquidation_price("PositionSide.LONG", Decimal("100"), 10)
    short_price = calc_liquidation_price("PositionSide.SHORT", Decimal("100"), 10)
    assert long_price == Decimal("91")
    assert short_price == Decimal("109")


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
    [(1, "90.00"), (10, "9.00"), (50, "1.80"), (125, "0.72"), (300, "0.30"), (None, "90.00")],
)
def test_move_pct_against_position(leverage, expected):
    assert liquidation_move_pct(leverage) == Decimal(expected)


def test_move_pct_is_displayed_without_trailing_zeros():
    assert fmt_leverage_move_pct(liquidation_move_pct(300)) == "0.3%"
    assert fmt_leverage_move_pct(liquidation_move_pct(50)) == "1.8%"
    assert fmt_leverage_move_pct(liquidation_move_pct(1)) == "90%"
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
        ("LONG", "100", "0.5"),     # плечо < 0.9 → цена ушла бы в минус
    ],
)
def test_no_price_instead_of_a_wrong_price(side, entry, leverage):
    assert calc_liquidation_price(side, entry, leverage) is None


def test_move_pct_guards():
    assert liquidation_move_pct(0) is None
    assert liquidation_move_pct("не число") is None
