"""Cliente del CLOB API de Polymarket: series historicas de precio.

Mientras Gamma da el precio actual, CLOB da la curva historica de un outcome.
OJO: el parametro `market` de este endpoint es el CLOB token ID (el id de un
outcome concreto), NO el market ID del Gamma.
"""
from __future__ import annotations

import logging
from typing import Any

from src.client.http import get_json

logger = logging.getLogger(__name__)

# No hardcodear URLs sueltas: constantes al inicio del modulo.
CLOB_BASE_URL: str = "https://clob.polymarket.com"
PRICES_HISTORY_ENDPOINT: str = f"{CLOB_BASE_URL}/prices-history"

VALID_INTERVALS: frozenset[str] = frozenset({"1h", "6h", "1d", "1w", "1m", "max"})


def get_price_history(
    clob_token_id: str,
    interval: str = "1d",
    fidelity: int = 60,
) -> list[dict[str, Any]]:
    """Devuelve la serie historica de precio de un outcome (CLOB token).

    Args:
        clob_token_id: id del outcome en el libro de ordenes (NO el market id).
        interval: ventana temporal (1h, 6h, 1d, 1w, 1m, max).
        fidelity: resolucion en minutos entre puntos.

    Returns:
        Lista de puntos {"t": unix_timestamp, "p": precio}, orden cronologico.
    """
    if interval not in VALID_INTERVALS:
        raise ValueError(f"interval invalido {interval!r}; usa uno de {sorted(VALID_INTERVALS)}")

    params = {"market": clob_token_id, "interval": interval, "fidelity": fidelity}
    payload = get_json(PRICES_HISTORY_ENDPOINT, params=params)

    # El endpoint envuelve la serie en {"history": [...]}.
    history = payload.get("history", []) if isinstance(payload, dict) else []
    logger.info(
        "prices-history token=%s interval=%s fidelity=%d -> %d puntos",
        clob_token_id, interval, fidelity, len(history),
    )
    return history


def _main() -> None:
    """Toma un token real del mercado de mayor volumen en Gamma y muestra su serie."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from src.client.gamma import flatten_markets, get_politics_events

    markets = flatten_markets(get_politics_events())
    markets = [m for m in markets if m["clob_token_ids"]]
    markets.sort(key=lambda m: m["volume_24h"] or 0, reverse=True)
    top = markets[0]
    token = top["clob_token_ids"][0]

    print(f"\nMercado: {top['question']}")
    print(f"Token (outcome '{top['outcomes'][0] if top['outcomes'] else '?'}'): {token}\n")

    history = get_price_history(token, interval="1w", fidelity=60)
    print(f"{len(history)} puntos. Primeros y ultimos 3:")
    for pt in history[:3] + history[-3:]:
        print(f"  t={pt.get('t')}  p={pt.get('p')}")


if __name__ == "__main__":
    _main()
