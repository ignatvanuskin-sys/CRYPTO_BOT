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

# Ликвидация: полная потеря маржи (как на реальной бирже).
# Позиция закрывается, когда убыток = 100% маржи.
# Единственный источник правды и для tp_sl_engine, и для отображения в UI.
LIQUIDATION_MARGIN_FRACTION = Decimal("1.0")


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
