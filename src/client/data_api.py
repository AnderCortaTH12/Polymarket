"""Cliente del Data API de Polymarket: trades, holders y posiciones.

Esta es la puerta a las "ballenas": trades individuales (con la wallet proxy del
usuario), mayores posiciones abiertas por mercado y la cartera completa de una
wallet. El Data API rechaza peticiones sin User-Agent (403); la cabecera la pone
por defecto `src.client.http`.
"""
from __future__ import annotations

import logging
from typing import Any

from src.client.http import get_json

logger = logging.getLogger(__name__)

# No hardcodear URLs sueltas: constantes al inicio del modulo.
DATA_BASE_URL: str = "https://data-api.polymarket.com"
TRADES_ENDPOINT: str = f"{DATA_BASE_URL}/trades"
HOLDERS_ENDPOINT: str = f"{DATA_BASE_URL}/holders"
POSITIONS_ENDPOINT: str = f"{DATA_BASE_URL}/positions"

DEFAULT_LIMIT: int = 100


def get_market_trades(condition_id: str, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """Ultimos trades de un mercado (incluye la proxyWallet de cada usuario).

    El endpoint usa el parametro `market`, que aqui es el condition_id del
    mercado (no el CLOB token id).
    """
    params = {"market": condition_id, "limit": limit}
    trades = get_json(TRADES_ENDPOINT, params=params)
    trades = trades if isinstance(trades, list) else []
    logger.info("trades market=%s -> %d trades", condition_id, len(trades))
    return trades


def get_market_holders(condition_id: str, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """Mayores posiciones abiertas de un mercado, agrupadas por token (outcome).

    Devuelve una lista de grupos `{"token": ..., "holders": [...]}`; cada holder
    trae `proxyWallet`, `amount` y `outcomeIndex`.
    """
    params = {"market": condition_id, "limit": limit}
    groups = get_json(HOLDERS_ENDPOINT, params=params)
    groups = groups if isinstance(groups, list) else []
    total = sum(len(g.get("holders", [])) for g in groups)
    logger.info("holders market=%s -> %d grupos, %d holders", condition_id, len(groups), total)
    return groups


PAGE_SIZE: int = 500  # maximo por pagina del endpoint de positions


def get_user_positions(proxy_wallet: str, max_positions: int | None = None) -> list[dict[str, Any]]:
    """Cartera COMPLETA (posiciones abiertas) de una wallet proxy, paginando.

    El endpoint devuelve como mucho `PAGE_SIZE` posiciones por llamada, asi que
    hay que recorrer paginas con `offset` hasta agotarlas; si no, se pierde la
    cola de la cartera y el total (usado para el % de cada posicion) queda mal.

    Cada posicion incluye tamaño, precio medio, valor actual y PnL.

    Args:
        proxy_wallet: wallet a consultar.
        max_positions: tope opcional de posiciones a traer (None = todas).
    """
    positions: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {"user": proxy_wallet, "limit": PAGE_SIZE, "offset": offset}
        page = get_json(POSITIONS_ENDPOINT, params=params)
        page = page if isinstance(page, list) else []
        positions.extend(page)
        if len(page) < PAGE_SIZE:
            break
        if max_positions is not None and len(positions) >= max_positions:
            break
        offset += PAGE_SIZE
    if max_positions is not None:
        positions = positions[:max_positions]
    logger.info("positions user=%s -> %d posiciones (paginado)", proxy_wallet, len(positions))
    return positions


def _main() -> None:
    """Demo: toma el mercado de mayor volumen del collector y sondea los 3 endpoints."""
    import sqlite3

    from src.collector.models import DB_PATH

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = sqlite3.connect(DB_PATH)
    cid, question = conn.execute(
        "SELECT condition_id, question FROM snapshots "
        "WHERE condition_id IS NOT NULL ORDER BY volume_24h DESC LIMIT 1"
    ).fetchone()
    conn.close()

    print(f"\nMercado: {question}\ncondition_id: {cid}\n")

    trades = get_market_trades(cid, limit=3)
    print("Ultimos trades:")
    for t in trades:
        print(f"  {t.get('side'):>4} {t.get('size')} @ {t.get('price')}  {t.get('outcome')}  {t.get('proxyWallet')}")

    holders = get_market_holders(cid, limit=3)
    print("\nTop holders:")
    for group in holders:
        for h in group.get("holders", [])[:3]:
            print(f"  {h.get('amount'):>12}  {h.get('outcomeIndex')}  {h.get('proxyWallet')}")

    if trades:
        wallet = trades[0]["proxyWallet"]
        positions = get_user_positions(wallet, max_positions=3)
        print(f"\nPosiciones de {wallet}:")
        for p in positions:
            print(f"  {p.get('title', '')[:40]:<40}  size={p.get('size')}  PnL={p.get('cashPnl')}")


if __name__ == "__main__":
    _main()
