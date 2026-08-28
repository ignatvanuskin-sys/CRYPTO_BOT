from decimal import Decimal

from bot.handlers.trade import safe_trade_error
from bot.keyboards import main_menu
from services.demo import DEMO_PRIZES


def test_demo_prizes_sum_exactly_to_pool():
    assert sum(DEMO_PRIZES, Decimal("0")) == Decimal("100.00")


def test_main_menu_has_product_navigation():
    labels = [button.text for row in main_menu().keyboard for button in row]
    assert "🚀 Торговать" in labels
    assert "💼 Позиции" in labels
    assert "🏆 Рейтинг" in labels
    assert "👤 Профиль" in labels
    assert "ℹ️ Как играть" in labels


def test_user_facing_trade_errors_are_not_raw_exceptions():
    assert safe_trade_error(RuntimeError("Market data stale")) == "⚠️ Рынок временно недоступен. Попробуй ещё раз через несколько секунд."
    assert safe_trade_error(RuntimeError("Insufficient margin")) == "⚠️ Недостаточно доступной маржи."
    assert safe_trade_error(RuntimeError("IntegrityError")) == "⚠️ Сделка не выполнена."
