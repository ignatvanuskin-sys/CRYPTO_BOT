"""Нейтральный слой форматирования — общий для `bot/` и `services/`.

Здесь нет ни aiogram, ни SQLAlchemy: только строки и Decimal. За счёт этого
`services/` не зависит от `bot/` (пуши о закрытии позиций отправляются без
импорта хендлеров), а формат денег, плеча и эмодзи существует в одном
экземпляре и не может разойтись между экранами бота и уведомлениями.

`bot/emojis.py` и `bot/views.py` реэкспортируют эти имена, поэтому хендлеры
продолжают импортировать их оттуда — как раньше.
"""

from __future__ import annotations

from decimal import Decimal

# ---------------------------------------------------------------- premium emoji
# Обычные эмодзи запрещены: только ID из разрешённого списка.

# LONG / SHORT — обязательные ID из ТЗ
LONG_EMOJI_ID = "5449683594425410231"
SHORT_EMOJI_ID = "5447183459602669338"

# Общие
WARNING_ID = "5420323339723881652"
QUESTION_ID = "5452069934089641166"
CHART_ID = "5231200819986047254"
CHART_UP_ID = "5244837092042750681"
CHART_DOWN_ID = "5246762912428603768"
CHECK_ID = "5206607081334906820"
CROSS_ID = "5210952531676504517"
MONEY_ID = "5409048419211682843"
RED_ID = "5411225014148014586"
GREEN_ID = "5416081784641168838"
FIRE_ID = "5424972470023104089"
BOOM_ID = "5276032951342088188"
MEGAPHONE_ID = "5424818078833715060"
STAR_ID = "5438496463044752972"
CROWN_ID = "5217822164362739968"
DIAMOND_ID = "5427168083074628963"
PIN_ID = "5397782960512444700"
BOOKMARK_ID = "5222444124698853913"
ENVELOPE_ID = "5253742260054409879"
LOCK_ID = "5296369303661067030"
GEAR_ID = "5341715473882955310"
CALENDAR_ID = "5413879192267805083"
BULB_ID = "5422439311196834318"
GOLD_ID = "5440539497383087970"
SILVER_ID = "5447203607294265305"
BRONZE_ID = "5453902265922376865"
SIREN_ID = "5395695537687123235"
PENCIL_ID = "5395444784611480792"
PARTY_ID = "5461151367559141950"
FREE_ID = "5406756500108501710"
FLAG_ID = "5460755126761312667"
PLUS_ID = "5397916757333654639"
PLAY_ID = "5264919878082509254"
LOCATION_ID = "5391032818111363540"
SOON_ID = "5440621591387980068"
BELL_ID = "5458603043203327669"
TRASH_ID = "5445267414562389170"
CANDLE_ID = "5451882707875276247"
BAN_ID = "5240241223632954241"
EXCLAMATION_ID = "5274099962655816924"
CLOWN_ID = "5269531045165816230"
SPEECH_ID = "5460795800101594035"
DESKTOP_ID = "5282843764451195532"
MUSIC_ID = "5463107823946717464"


def tg_emoji(emoji_id: str, fallback: str = "✨") -> str:
    """HTML-тег для premium-эмодзи в сообщениях (parse_mode=HTML)."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


# Готовые теги для частых случаев
TG_LONG = tg_emoji(LONG_EMOJI_ID, "🔼")
TG_SHORT = tg_emoji(SHORT_EMOJI_ID, "🔽")
TG_WARNING = tg_emoji(WARNING_ID, "⚠️")
TG_CHECK = tg_emoji(CHECK_ID, "✔️")
TG_CROSS = tg_emoji(CROSS_ID, "❌")
TG_MONEY = tg_emoji(MONEY_ID, "💵")
TG_CHART = tg_emoji(CHART_ID, "📊")
TG_CHART_UP = tg_emoji(CHART_UP_ID, "📈")
TG_CROWN = tg_emoji(CROWN_ID, "👑")
TG_DIAMOND = tg_emoji(DIAMOND_ID, "💎")
TG_PARTY = tg_emoji(PARTY_ID, "🎉")
TG_PIN = tg_emoji(PIN_ID, "📌")
TG_GREEN = tg_emoji(GREEN_ID, "🟢")
TG_RED = tg_emoji(RED_ID, "🔴")
TG_QUESTION = tg_emoji(QUESTION_ID, "❓")
TG_BULB = tg_emoji(BULB_ID, "💡")
TG_GEAR = tg_emoji(GEAR_ID, "⚙️")
TG_SIREN = tg_emoji(SIREN_ID, "🚨")


# ------------------------------------------------------------------- форматтеры


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


def fmt_pct(value: Decimal | int | float | None) -> str:
    if value is None:
        return "—"
    return f"{Decimal(str(value)):+.2f}%"


def fmt_signed_money(value: Decimal | int | float | None) -> str:
    """Деньги со знаком: '+$125.30' / '-$98.00'. Для PnL в пушах и карточках."""
    if value is None:
        return "—"
    d = Decimal(str(value))
    return f"+{fmt_money(d)}" if d > 0 else fmt_money(d)


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
