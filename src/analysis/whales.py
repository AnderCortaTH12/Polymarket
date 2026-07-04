"""Identificacion de ballenas en Polymarket Politics (FASE 5).

Rankea las wallets con mayor exposicion en politica agregando los mayores
holders de un conjunto de mercados, y cruza cada wallet con su historial
on-chain via Polygonscan para detectar wallets que comparten la misma fuente
de fondos (posible misma persona/entidad tras varias cuentas).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from src.client.data_api import get_market_holders
from src.client.polygonscan import get_first_token_transfers, get_first_transactions

logger = logging.getLogger(__name__)


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
def get_wallet_funding_source(wallet_address: str) -> tuple[str, str] | None:
    """Wallet que financio originalmente a `wallet_address`, via Polygonscan.

    Metodo principal: primera transferencia ENTRANTE de USDC.e (asi se financian
    las proxy wallets de Polymarket). Fallback: primera transaccion nativa
    entrante (MATIC), por si la wallet se fondeo de otra forma.

    Returns:
        Tupla (funder_address, metodo) donde metodo es "usdc" o "native"; o
        None si no se encuentra ninguna transaccion entrante.
    """
    transfers = get_first_token_transfers(wallet_address, limit=1)
    if transfers:
        return transfers[0].get("from"), "usdc"

    txs = get_first_transactions(wallet_address, limit=1)
    if txs:
        return txs[0].get("from"), "native"

    logger.info("Wallet %s sin entradas (USDC ni nativas); sin funding source", wallet_address)
    return None


def group_by_funding_source(whales: list[Whale]) -> dict[str, list[str]]:
    """Agrupa wallets que comparten la misma fuente de fondos.

    Para cada ballena obtiene su funding source (independientemente del metodo,
    USDC o nativo) y agrupa por la direccion del funder. Devuelve solo los
    grupos con mas de una wallet: los interesantes, porque sugieren una misma
    persona/entidad operando varias cuentas.
    """
    by_source: dict[str, list[str]] = defaultdict(list)
    for whale in whales:
        result = get_wallet_funding_source(whale.proxy_wallet)
        if result:
            funder, _method = result
            by_source[funder].append(whale.proxy_wallet)
    clusters = {src: wallets for src, wallets in by_source.items() if len(wallets) > 1}
    logger.info("%d wallets -> %d clusters de funding compartido", len(whales), len(clusters))
    return clusters


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
