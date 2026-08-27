from decimal import Decimal

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
