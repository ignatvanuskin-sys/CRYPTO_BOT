from decimal import Decimal, InvalidOperation

def calc_pnl(side: str, entry: Decimal, exit: Decimal, qty: Decimal) -> Decimal:
    if side == "LONG":
        gross = (exit - entry) * qty
    else:  # SHORT
        gross = (entry - exit) * qty
    return gross.quantize(Decimal("0.01"))

def calc_unrealized(side: str, entry: Decimal, current: Decimal, qty: Decimal) -> Decimal:
    return calc_pnl(side, entry, current, qty)

def calc_notional(price: Decimal, qty: Decimal) -> Decimal:
    return (price * qty).quantize(Decimal("0.01"))

# ── Изолированная маржа (legacy, оставлена для совместимости и тестов) ──
# Позиция закрывалась, когда убыток = 100% её маржи.
# Единственный источник для старых изолированных расчётов.
LIQUIDATION_MARGIN_FRACTION = Decimal("1.0")

# ── Кросс-маржа (актуальная модель, по ТЗ заказчика) ──
# Формулировка: «ликвидация для всего бюджета — пока общий депозит не останется 0».
# Все открытые позиции используют общий пул (весь баланс счёта).
# Форс-закрытие ВСЕХ позиций когда equity исчерпано на 90% депозита.
# Депозит = initial_balance — ФИКСИРОВАННЫЙ стартовый депозит турнира (обычно $10 000),
#           НЕ текущий пик equity и НЕ cash+margin. Порог = initial × 0.10.
#           Это сознательный выбор: пользователь, нарастивший баланс до $15 000,
#           может потерять $14 000 прибыли до срабатывания защиты (до $1 000).
#           Если бы порог считался от пика (trailing), успешные пользователи
#           ликвидировались бы раньше. Заказчик формулировал «пока общий депозит
#           не останется 0» — читается как исходный депозит, поэтому фиксируем
#           именно такую семантику. Подтверждено перед показом.
# Порог = 10% депозита остаётся, 90% съедено — есть запас на гэп между тиками движка
# (движок тикает 1с). База расчёта — весь депозит, не одна позиция.
# ACCOUNT_LIQUIDATION_REMAINING — сколько депозита должно остаться, остальное = буфер 90%.
ACCOUNT_LIQUIDATION_REMAINING_FRACTION = Decimal("0.10")
CROSS_LIQUIDATION_BUFFER_FRACTION = Decimal("0.90")


def _safe_leverage(leverage: Decimal | int | None) -> Decimal | None:
    """None → плечо 1 (не задано). 0 / отрицательное / мусор → None (невалидно).

    `leverage or 1` здесь нельзя: 0 — falsy и незаметно превратился бы в 1,
    дав цену ликвидации для несуществующего плеча.
    """
    try:
        lev = Decimal(str(1 if leverage is None else leverage))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not lev.is_finite() or lev <= 0:
        return None
    return lev


def calc_margin(notional: Decimal, leverage: Decimal | int | None) -> Decimal:
    # None = плечо не задано → x1. Невалидное (0/мусор) тоже безопасно трактуем как x1:
    # маржа = весь notional. Строгий None-результат дают calc_liquidation_price / liquidation_move_pct.
    lev = _safe_leverage(leverage) or Decimal(1)
    return (Decimal(str(notional)) / lev).quantize(Decimal("0.01"))


def liquidation_threshold_pnl(notional: Decimal, leverage: Decimal | int | None) -> Decimal:
    """Порог unrealized PnL, при котором позиция ликвидируется (отрицательное число)."""
    return -calc_margin(notional, leverage) * LIQUIDATION_MARGIN_FRACTION


def is_liquidated(unrealized_pnl: Decimal, notional: Decimal, leverage: Decimal | int | None) -> bool:
    return Decimal(str(unrealized_pnl)) <= liquidation_threshold_pnl(notional, leverage)


def calc_liquidation_price(
    side: str,
    entry: Decimal,
    leverage: Decimal | int | None,
    quantity: Decimal | None = None,
    notional: Decimal | None = None,
) -> Decimal | None:
    """Цена, при которой unrealized PnL достигает порога ликвидации.

    Обратная функция к проверке в tp_sl_engine:
        LONG:  (P − E) × Q = −margin × 1.0
        SHORT: (E − P) × Q = −margin × 1.0

    Если переданы quantity и notional (открытая позиция) — считаем от них,
    ровно как движок. Иначе — оценка до открытия: E × (1 ∓ 1.0 / L).
    Возвращает None, если данных не хватает или цена ушла бы в ноль.
    """
    try:
        entry = Decimal(str(entry))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not entry.is_finite() or entry <= 0:
        return None
    lev = _safe_leverage(leverage)
    if lev is None:
        return None
    is_long = str(side).upper().endswith("LONG")

    if quantity is not None and notional is not None:
        try:
            qty = Decimal(str(quantity))
            notl = Decimal(str(notional))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if qty.is_finite() and notl.is_finite() and qty > 0 and notl > 0:
            # threshold отрицательный → для LONG цена ниже входа, для SHORT выше
            delta = liquidation_threshold_pnl(notl, lev) / qty
            price = entry + delta if is_long else entry - delta
            return price if price > 0 else None

    move = entry * LIQUIDATION_MARGIN_FRACTION / lev
    price = entry - move if is_long else entry + move
    return price if price > 0 else None


def liquidation_move_pct(leverage: Decimal | int | None) -> Decimal | None:
    """На сколько процентов цена должна уйти против позиции до ликвидации."""
    lev = _safe_leverage(leverage)
    if lev is None:
        return None
    return (LIQUIDATION_MARGIN_FRACTION / lev * Decimal("100")).quantize(Decimal("0.01"))


# ── Кросс-маржевые хелперы — единый источник и для движка, и для UI ──

def cross_liquidation_threshold(initial_balance: Decimal | int | float | None) -> Decimal:
    """Порог equity, ниже которого кросс-ликвидация (10% депозита остаётся).
    initial_balance — ФИКСИРОВАННЫЙ стартовый депозит турнира (обычно $10 000),
    не текущий пик equity. См. комментарий к ACCOUNT_LIQUIDATION_REMAINING_FRACTION.
    """
    try:
        init = Decimal(str(initial_balance if initial_balance is not None else 0))
    except (InvalidOperation, TypeError, ValueError):
        init = Decimal("0")
    if not init.is_finite() or init < 0:
        init = Decimal("0")
    return (init * ACCOUNT_LIQUIDATION_REMAINING_FRACTION).quantize(Decimal("0.01"))


def is_account_liquidated(equity: Decimal | int | float | None, initial_balance: Decimal | int | float | None) -> bool:
    """Кросс-критерий: equity <= 10% ФИКСИРОВАННОГО initial ⇒ все позиции закрываются.
    initial — стартовый депозит, не пик. Даёт 90% буфер от исходного депозита.
    """
    try:
        eq = Decimal(str(equity if equity is not None else 0))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return eq <= cross_liquidation_threshold(initial_balance)


def cross_liquidation_buffer(equity: Decimal | int | float | None, initial_balance: Decimal | int | float | None) -> Decimal:
    """Сколько $ осталось до кросс-ликвидации (equity - threshold). Может быть <0 если уже пробито."""
    try:
        eq = Decimal(str(equity if equity is not None else 0))
    except (InvalidOperation, TypeError, ValueError):
        eq = Decimal("0")
    return (eq - cross_liquidation_threshold(initial_balance)).quantize(Decimal("0.01"))


def cross_liquidation_buffer_pct(equity: Decimal | int | float | None, initial_balance: Decimal | int | float | None) -> Decimal:
    """Запас до ликвидации в % депозита (buffer / initial *100). 100% = весь депозит цел, 0% = порог."""
    try:
        buf = cross_liquidation_buffer(equity, initial_balance)
        init = Decimal(str(initial_balance if initial_balance is not None else 0))
        if not init.is_finite() or init == 0:
            return Decimal("0.00")
        return (buf / init * Decimal("100")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")
