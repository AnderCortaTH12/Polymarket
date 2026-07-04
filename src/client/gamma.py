"""Cliente del Gamma API de Polymarket.

Gamma expone el catálogo de eventos y mercados (precios, volumen, liquidez),
filtrable por `tag_slug=politics`. Aquí obtenemos los eventos activos de
política y los aplanamos a una fila por mercado, parseando en el cliente los
campos que Polymarket entrega como strings JSON.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from src.client.http import get_json

logger = logging.getLogger(__name__)

# No hardcodear URLs sueltas: constantes al inicio del módulo.
GAMMA_BASE_URL: str = "https://gamma-api.polymarket.com"
EVENTS_ENDPOINT: str = f"{GAMMA_BASE_URL}/events"

# Tamaño de página para la paginación por offset.
PAGE_LIMIT: int = 100
POLYMARKET_MARKET_URL: str = "https://polymarket.com/event"


def get_politics_events(page_limit: int = PAGE_LIMIT) -> list[dict[str, Any]]:
    """Descarga todos los eventos de política activos, paginando por offset.

    Filtra por `tag_slug=politics`, activos y no cerrados, ordenados por volumen
    de 24h descendente. Sigue pidiendo páginas hasta que una devuelve menos de
    `page_limit` resultados (última página).
    """
    events: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {
            "tag_slug": "politics",
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            "limit": page_limit,
            "offset": offset,
        }
        page = get_json(EVENTS_ENDPOINT, params=params)
        if not isinstance(page, list):
            logger.warning("Respuesta inesperada de Gamma (no es lista): %r", type(page))
            break
        events.extend(page)
        logger.info("Página offset=%d: %d eventos (acumulado %d)", offset, len(page), len(events))
        if len(page) < page_limit:
            break
        offset += page_limit
    return events


def _parse_json_field(raw: Any) -> Any:
    """Parsea un campo que Gamma puede entregar como string JSON.

    Campos como `outcomes`, `outcomePrices` o `clobTokenIds` llegan como
    `'["0.62","0.38"]'`. Si ya viene parseado (lista), se devuelve tal cual.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    return raw


def _to_float(value: Any) -> float | None:
    """Convierte un valor a float de forma segura (None si no se puede)."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def flatten_markets(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aplana los eventos a una fila por mercado.

    Cada evento contiene N mercados. Parsea los campos que vienen como strings
    JSON (`outcomes`, `outcomePrices`, `clobTokenIds`) y extrae los campos
    útiles para el resto del sistema.
    """
    rows: list[dict[str, Any]] = []
    for event in events:
        event_slug = event.get("slug")
        event_title = event.get("title")
        for market in event.get("markets", []):
            outcomes = _parse_json_field(market.get("outcomes")) or []
            prices_raw = _parse_json_field(market.get("outcomePrices")) or []
            outcome_prices = [_to_float(p) for p in prices_raw]
            clob_token_ids = _parse_json_field(market.get("clobTokenIds")) or []

            rows.append({
                "market_id": market.get("id"),
                "condition_id": market.get("conditionId"),
                "event_slug": event_slug,
                "event_title": event_title,
                "question": market.get("question"),
                "outcomes": outcomes,
                "outcome_prices": outcome_prices,
                "clob_token_ids": clob_token_ids,
                "best_bid": _to_float(market.get("bestBid")),
                "best_ask": _to_float(market.get("bestAsk")),
                "spread": _to_float(market.get("spread")),
                "last_trade_price": _to_float(market.get("lastTradePrice")),
                "volume_24h": _to_float(market.get("volume24hr")),
                "volume_total": _to_float(market.get("volume")),
                "liquidity": _to_float(market.get("liquidity")),
                "end_date": market.get("endDate"),
                "url": f"{POLYMARKET_MARKET_URL}/{event_slug}" if event_slug else None,
            })
    return rows


def _main() -> None:
    """Descarga eventos de política e imprime los 10 mercados con más volumen 24h."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    events = get_politics_events()
    markets = flatten_markets(events)
    markets.sort(key=lambda m: m["volume_24h"] or 0, reverse=True)

    print(f"\n{len(events)} eventos, {len(markets)} mercados. Top 10 por volumen 24h:\n")
    for m in markets[:10]:
        prob = ""
        if m["outcomes"] and m["outcome_prices"] and m["outcome_prices"][0] is not None:
            prob = f"  {m['outcomes'][0]}={m['outcome_prices'][0]:.0%}"
        vol = m["volume_24h"] or 0
        print(f"  ${vol:>12,.0f}  {(m['question'] or '')[:60]:<60}{prob}")


if __name__ == "__main__":
    _main()
