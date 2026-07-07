"""Calculo de P&L (ganancia/perdida) de una alerta al precio actual del mercado.

Funciones puras (sin red ni Streamlit): el precio actual del outcome se pasa
como argumento (el dashboard lo obtiene de Gamma y lo cachea). Asi el calculo
es facilmente testeable.
"""
from __future__ import annotations

from typing import Any


def compute_pnl(
    price_at_detection: float,
    trade_size_usd: float,
    trade_side: str,
    current_price: float,
) -> tuple[float, float]:
    """P&L en $ y en % de una posicion, al precio actual del outcome.

    shares = trade_size_usd / price_at_detection. Para una COMPRA (BUY) del
    outcome, la ganancia por share es (precio_actual - precio_entrada); para una
    VENTA (SELL) se invierte. `current_price` es el precio actual DEL MISMO
    outcome de la posicion (Yes o No), asi que la logica Yes/No ya esta implicita
    en que precio se consulta (no hay que invertir de nuevo aqui).

    Returns:
        (pnl_usd, pnl_pct).
    """
    shares = trade_size_usd / price_at_detection
    delta = current_price - price_at_detection
    if str(trade_side).upper() == "SELL":
        delta = -delta
    pnl = delta * shares
    pnl_pct = (delta / price_at_detection) * 100.0
    return pnl, pnl_pct


def format_pnl_cell(
    price_at_detection: Any,
    trade_size_usd: Any,
    trade_side: Any,
    state: dict[str, Any],
) -> str:
    """Texto de la celda P&L a partir del estado de precio del mercado.

    `state` = {"status": "active"|"resolved"|"na", "price": float|None}.
    - active:   "+$X.XX (+Y.Y%)" / "-$X.XX (-Y.Y%)" / "$0.00 (0%)"
    - resolved: "Resuelto ($payout)"  (payout = shares * precio_final 0/1)
    - na / datos invalidos: "N/A"
    El color (verde/rojo/gris) lo aplica el dashboard segun el prefijo.
    """
    try:
        detection = float(price_at_detection)
        size = float(trade_size_usd)
    except (TypeError, ValueError):
        return "N/A"
    if detection <= 0:
        return "N/A"

    status = state.get("status")
    price = state.get("price")
    if status == "na" or price is None:
        return "N/A"

    shares = size / detection
    if status == "resolved":
        payout = shares * float(price)  # precio final 0 o 1
        return f"Resuelto (${payout:,.0f})"

    pnl, pnl_pct = compute_pnl(detection, size, trade_side, float(price))
    if abs(pnl) < 0.005:
        return "$0.00 (0%)"
    sign = "+" if pnl > 0 else "-"
    return f"{sign}${abs(pnl):,.2f} ({pnl_pct:+.1f}%)"
