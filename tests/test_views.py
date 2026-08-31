"""bot/views.py — main_menu, back_keyboard, bingx_chart_url, safe_edit."""
from bot.views import main_menu, back_keyboard, bingx_chart_url, btn, fmt_money


class TestMainMenu:
    def test_buttons(self):
        kb = main_menu()
        labels = [b.text for row in kb.keyboard for b in row]
        # Spec 4 primary + secondary per new UX
        assert labels == ["Торговать", "Мои позиции", "Лидерборд", "Профиль", "История", "Соревнование", "Помощь"]

    def test_premium_icons(self):
        kb = main_menu()
        for row in kb.keyboard:
            for b in row:
                assert b.icon_custom_emoji_id is not None

    def test_resize(self):
        kb = main_menu()
        assert kb.resize_keyboard is True
        assert kb.is_persistent is True


class TestBackKeyboard:
    def test_default(self):
        kb = back_keyboard()
        assert kb.inline_keyboard[0][0].callback_data == "nav:home"

    def test_custom_target(self):
        kb = back_keyboard("nav:trade")
        assert kb.inline_keyboard[0][0].callback_data == "nav:trade"


class TestBtn:
    def test_with_style_and_icon(self):
        b = btn("Test", "cb:1", icon="123", style="danger")
        assert b.text == "Test"
        assert b.callback_data == "cb:1"
        assert b.icon_custom_emoji_id == "123"
        assert b.style == "danger"

    def test_no_icon_no_style(self):
        b = btn("Plain", "cb:2")
        assert b.text == "Plain"
        assert b.callback_data == "cb:2"


class TestBingxChartUrl:
    def test_sol(self): assert bingx_chart_url("SOLUSDT") == "https://bingx.com/en/perpetual/SOL-USDT"
    def test_btc(self): assert bingx_chart_url("BTCUSDT") == "https://bingx.com/en/perpetual/BTC-USDT"
    def test_lowercase(self): assert bingx_chart_url("sol") == "https://bingx.com/en/perpetual/SOL-USDT"