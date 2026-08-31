from decimal import Decimal

from bot.emojis import CHART_ID, CHART_UP_ID, CROWN_ID, GOLD_ID, STAR_ID, WARNING_ID
from bot.handlers.trade import normalize_ticker, safe_trade_error
from bot.views import bingx_chart_url, main_menu
from services.demo import DEMO_PRIZES


def test_demo_prizes_sum_exactly_to_pool():
    assert sum(DEMO_PRIZES, Decimal("0")) == Decimal("100.00")


def test_main_menu_has_reference_navigation():
    kb = main_menu()
    labels = [button.text for row in kb.keyboard for button in row]
    # Spec 4 primary
    assert "Торговать" in labels
    assert "Мои позиции" in labels
    assert "Лидерборд" in labels
    assert "Профиль" in labels
    # secondary per spec
    assert "История" in labels
    # premium icons via icon_custom_emoji_id, not regular emojis in text
    icons = [button.icon_custom_emoji_id for row in kb.keyboard for button in row]
    assert CHART_UP_ID in icons
    assert CROWN_ID in icons
    assert GOLD_ID in icons
    assert CHART_ID in icons
    # ensure no regular emoji remains in button texts
    assert all("🚀" not in t and "👤" not in t for t in labels)


def test_user_facing_trade_errors_are_not_raw_exceptions():
    assert safe_trade_error(RuntimeError("Market data stale")) == f'<tg-emoji emoji-id="{WARNING_ID}">⚠️</tg-emoji> Рынок временно недоступен. Попробуйте ещё раз через несколько секунд.'
    assert safe_trade_error(RuntimeError("Insufficient margin")) == f'<tg-emoji emoji-id="{WARNING_ID}">⚠️</tg-emoji> Недостаточно доступной маржи.'
    assert safe_trade_error(RuntimeError("IntegrityError")) == f'<tg-emoji emoji-id="{WARNING_ID}">⚠️</tg-emoji> Сделка не выполнена.'


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


def test_premium_long_short_emoji_ids():
    from bot.emojis import LONG_EMOJI_ID, SHORT_EMOJI_ID

    assert LONG_EMOJI_ID == "5449683594425410231"
    assert SHORT_EMOJI_ID == "5447183459602669338"
