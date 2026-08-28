from decimal import Decimal

from bot.handlers.trade import normalize_ticker, safe_trade_error
from bot.views import bingx_chart_url, main_menu
from services.demo import DEMO_PRIZES


def test_demo_prizes_sum_exactly_to_pool():
    assert sum(DEMO_PRIZES, Decimal("0")) == Decimal("100.00")


def test_main_menu_has_reference_navigation():
    labels = [button.text for row in main_menu().keyboard for button in row]
    assert "🚀 Торговать" in labels
    assert "👤 Личный кабинет" in labels


def test_user_facing_trade_errors_are_not_raw_exceptions():
    assert safe_trade_error(RuntimeError("Market data stale")) == "⚠️ Рынок временно недоступен. Попробуйте ещё раз через несколько секунд."
    assert safe_trade_error(RuntimeError("Insufficient margin")) == "⚠️ Недостаточно доступной маржи."
    assert safe_trade_error(RuntimeError("IntegrityError")) == "⚠️ Сделка не выполнена."


def test_ticker_normalization_matches_reference_example():
    assert normalize_ticker("SOL") == "SOLUSDT"
    assert normalize_ticker("sol") == "SOLUSDT"
    assert normalize_ticker("SOLUSDT") == "SOLUSDT"
    assert normalize_ticker("sol-usdt") == "SOLUSDT"
    assert normalize_ticker("BTC/USDT") == "BTCUSDT"
    assert normalize_ticker("!@#") is None
    assert normalize_ticker("") is None


def test_bingx_chart_link_format():
    assert bingx_chart_url("SOLUSDT") == "https://bingx.com/en/perpetual/SOL-USDT"
    assert bingx_chart_url("BTCUSDT") == "https://bingx.com/en/perpetual/BTC-USDT"
