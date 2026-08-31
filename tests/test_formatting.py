"""services/formatting.py — все форматтеры, premium emoji IDs, btn()."""
from decimal import Decimal

import pytest

from services.formatting import (
    LONG_EMOJI_ID, SHORT_EMOJI_ID, GOLD_ID, CHART_UP_ID,
    fmt_money, fmt_price, fmt_pct, fmt_leverage, fmt_leverage_move_pct,
    fmt_signed_money, format_side, tg_emoji,
    TG_LONG, TG_SHORT, TG_WARNING, TG_CHECK,
)


class TestFmtMoney:
    def test_positive(self): assert fmt_money(Decimal("100")) == "$100.00"
    def test_negative(self): assert fmt_money(Decimal("-50.5")) == "$-50.50"
    def test_zero(self): assert fmt_money(Decimal("0")) == "$0.00"
    def test_none(self): assert fmt_money(None) == "—"
    def test_int(self): assert fmt_money(42) == "$42.00"
    def test_large(self): assert fmt_money(Decimal("1234567.89")) == "$1,234,567.89"
    def test_float(self): assert fmt_money(3.14) == "$3.14"
    def test_string(self): assert fmt_money("99.99") == "$99.99"


class TestFmtPrice:
    def test_btc(self): assert fmt_price(Decimal("78150.30")) == "$78,150.30"
    def test_sol(self): assert fmt_price(Decimal("102.70")) == "$102.7000"
    def test_ub(self): assert fmt_price(Decimal("0.14")) == "$0.140000"
    def test_tiny(self): assert fmt_price(Decimal("0.000008")) == "$0.00000800"
    def test_none(self): assert fmt_price(None) == "—"
    def test_precision_2(self): assert fmt_price(Decimal("105.123456"), precision=2) == "$105.12"
    def test_precision_5(self): assert fmt_price(Decimal("0.14235"), precision=5) == "$0.14235"
    def test_zero(self): assert fmt_price(Decimal("0")) == "$0.00"


class TestFmtPct:
    def test_positive(self): assert fmt_pct(Decimal("26.6194")) == "+26.62%"
    def test_negative(self): assert fmt_pct(Decimal("-1.7")) == "-1.70%"
    def test_zero(self): assert fmt_pct(Decimal("0")) == "+0.00%"
    def test_none(self): assert fmt_pct(None) == "—"


class TestFmtLeverage:
    def test_int(self): assert fmt_leverage(50) == "x50"
    def test_decimal_int(self): assert fmt_leverage(Decimal("50.00")) == "x50"
    def test_decimal_frac(self): assert fmt_leverage(Decimal("2.50")) == "x2.5"
    def test_300(self): assert fmt_leverage(300) == "x300"
    def test_none(self): assert fmt_leverage(None) == "x1"
    def test_1(self): assert fmt_leverage(1) == "x1"


class TestFmtLeverageMovePct:
    """fmt_leverage_move_pct — форматтер: принимает РЕЗУЛЬТАТ liquidation_move_pct."""
    def test_50x(self): assert fmt_leverage_move_pct(Decimal("1.80")) == "1.8%"
    def test_300x(self): assert fmt_leverage_move_pct(Decimal("0.30")) == "0.3%"
    def test_1x(self): assert fmt_leverage_move_pct(Decimal("90.00")) == "90%"
    def test_10x(self): assert fmt_leverage_move_pct(Decimal("9.00")) == "9%"
    def test_none(self): assert fmt_leverage_move_pct(None) == "—"


class TestFmtSignedMoney:
    def test_positive(self): assert fmt_signed_money(Decimal("125.30")) == "+$125.30"
    def test_negative(self): assert fmt_signed_money(Decimal("-98.00")) == "$-98.00"
    def test_zero(self): assert fmt_signed_money(Decimal("0")) == "$0.00"
    def test_none(self): assert fmt_signed_money(None) == "—"


class TestFormatSide:
    def test_string(self): assert format_side("LONG") == "LONG"
    def test_enum(self):
        from db.paper_models import PositionSide
        assert format_side(PositionSide.LONG) == "LONG"
    def test_enum_full(self): assert format_side("PositionSide.SHORT") == "SHORT"
    def test_none(self): assert format_side(None) == "—"


class TestTgEmoji:
    def test_format(self): assert tg_emoji("123", "🔥") == '<tg-emoji emoji-id="123">🔥</tg-emoji>'
    def test_default(self): assert tg_emoji("456") == '<tg-emoji emoji-id="456">✨</tg-emoji>'
    def test_premium_ids_nonempty(self):
        for eid in [LONG_EMOJI_ID, SHORT_EMOJI_ID, GOLD_ID, CHART_UP_ID]:
            assert isinstance(eid, str) and len(eid) > 0


class TestTgConstants:
    def test_long_tag(self): assert "5449683594425410231" in TG_LONG
    def test_short_tag(self): assert "5447183459602669338" in TG_SHORT
    def test_warning_tag(self): assert "5420323339723881652" in TG_WARNING