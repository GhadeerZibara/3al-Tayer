def price_line(product, qty, unit, tier):
    """Prices are per piece in the sheet. Returns (pieces, unit_price, line_total)."""
    unit = unit or "piece"
    per_piece = float(product[f"price_usd_{tier}"])
    upc = int(product["units_per_carton"] or 1)
    pieces = qty * upc if unit == "carton" else qty
    unit_price = per_piece * upc if unit == "carton" else per_piece
    return pieces, round(unit_price, 2), round(unit_price * qty, 2)

def available(stock_row):
    return int(stock_row["on_hand"] or 0) - int(stock_row["reserved"] or 0)

def to_lbp(usd, rate, rounding):
    return int(round(usd * rate / rounding) * rounding)