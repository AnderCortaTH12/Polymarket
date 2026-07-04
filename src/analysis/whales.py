"""Identificacion de ballenas en Polymarket Politics (FASE 5).

Parte funcional: agregar los mayores holders de un conjunto de mercados para
rankear las wallets con mayor exposicion en politica. Parte esqueleto (se
detalla mas adelante): cruzar cada wallet con su historial on-chain via
Polygonscan y detectar wallets que comparten la misma fuente de fondos.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from src.client.data_api import get_market_holders

logger = logging.getLogger(__name__)

# Clave gratuita de Polygonscan para el cruce on-chain (ver .env.example).
POLYGONSCAN_API_KEY_ENV: str = "POLYGONSCAN_API_KEY"


@dataclass
class Whale:
    """Wallet agregada por su exposicion total en los mercados analizados."""

    proxy_wallet: str
    total_amount: float = 0.0
    markets: set[str] = field(default_factory=set)  # condition_ids donde aparece


def rank_whales(condition_ids: list[str], top_n: int = 20) -> list[Whale]:
    """Rankea wallets por exposicion agregada a traves de varios mercados.

    Suma el `amount` de cada holder en todos los mercados dados y ordena de
    mayor a menor. Devuelve las `top_n` wallets con mas exposicion.
    """
    acc: dict[str, Whale] = {}
    for cid in condition_ids:
        for group in get_market_holders(cid):
            for holder in group.get("holders", []):
                wallet = holder.get("proxyWallet")
                amount = holder.get("amount")
                if not wallet or amount is None:
                    continue
                whale = acc.setdefault(wallet, Whale(proxy_wallet=wallet))
                whale.total_amount += float(amount)
                whale.markets.add(cid)

    ranked = sorted(acc.values(), key=lambda w: w.total_amount, reverse=True)
    logger.info("Rankeadas %d wallets en %d mercados", len(ranked), len(condition_ids))
    return ranked[:top_n]


# --------------------------------------------------------------------------- #
# Esqueleto — cruce on-chain (se implementa cuando tengamos la API key)
# --------------------------------------------------------------------------- #
def get_wallet_funding_source(proxy_wallet: str) -> str | None:
    """[ESQUELETO] Wallet que fondeo por primera vez a `proxy_wallet` (via Polygonscan).

    Requiere POLYGONSCAN_API_KEY. Pendiente de implementar en FASE 5 detallada:
    consultar la primera transaccion entrante de la wallet en Polygon.
    """
    if not os.getenv(POLYGONSCAN_API_KEY_ENV):
        logger.warning("Falta %s; el cruce on-chain no esta disponible aun.", POLYGONSCAN_API_KEY_ENV)
    raise NotImplementedError("Cruce on-chain via Polygonscan pendiente (FASE 5 detallada)")


def group_by_funding_source(whales: list[Whale]) -> dict[str, list[str]]:
    """[ESQUELETO] Agrupa wallets que comparten la misma fuente de fondos.

    Detectaria clusters (posible misma persona/entidad tras varias wallets).
    Pendiente hasta tener `get_wallet_funding_source` operativo.
    """
    raise NotImplementedError("Deteccion de funding compartido pendiente (FASE 5 detallada)")


def _main() -> None:
    """Demo: rankea ballenas sobre los mercados de mayor volumen del collector."""
    import sqlite3

    from src.collector.models import DB_PATH

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = sqlite3.connect(DB_PATH)
    cids = [r[0] for r in conn.execute(
        "SELECT DISTINCT condition_id FROM snapshots "
        "WHERE condition_id IS NOT NULL ORDER BY volume_24h DESC LIMIT 5"
    ).fetchall()]
    conn.close()

    whales = rank_whales(cids, top_n=10)
    print(f"\nTop {len(whales)} ballenas en {len(cids)} mercados de mayor volumen:\n")
    for w in whales:
        print(f"  {w.total_amount:>14,.0f}  {w.proxy_wallet}  ({len(w.markets)} mercados)")


if __name__ == "__main__":
    _main()
