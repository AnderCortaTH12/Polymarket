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
from typing import Any

from src.client.data_api import get_market_holders
from src.client.polygonscan import get_first_token_transfers, get_first_transactions

logger = logging.getLogger(__name__)


# Cuantos mercados (por volumen) se agregan como maximo para rankear. Iterar
# TODOS los mercados de politica activos serian miles de llamadas a la Data API;
# las ballenas estan donde hay liquidez, asi que basta con los de mas volumen.
DEFAULT_MAX_MARKETS: int = 60


@dataclass
class Whale:
    """Wallet agregada por su VALOR EN DOLARES en los mercados analizados."""

    proxy_wallet: str
    total_value_usd: float = 0.0  # sum(shares * precio actual del outcome)
    markets: set[str] = field(default_factory=set)  # condition_ids donde aparece


def rank_whales(
    markets: list[dict[str, Any]],
    top_n: int = 50,
    max_markets: int = DEFAULT_MAX_MARKETS,
) -> list[Whale]:
    """Rankea wallets por VALOR EN DOLARES agregado a traves de varios mercados.

    El valor de cada posicion es `amount` (nº de shares del outcome) multiplicado
    por el PRECIO ACTUAL de ese outcome, no el nº de shares en bruto: dos
    posiciones con el mismo nº de shares valen distinto si el precio difiere.

    Recibe los mercados ya aplanados (de `flatten_markets`, que trae
    `outcome_prices`), toma los `max_markets` de mayor volumen 24h (ahi estan las
    ballenas), agrega los holders y ordena por valor descendente.

    Args:
        markets: mercados aplanados con `condition_id` y `outcome_prices`.
        top_n: cuantas ballenas devolver.
        max_markets: tope de mercados a consultar (por volumen) para acotar la API.
    """
    con_markets = [m for m in markets if m.get("condition_id")]
    con_markets.sort(key=lambda m: m.get("volume_24h") or 0, reverse=True)
    sample = con_markets[:max_markets]

    acc: dict[str, Whale] = {}
    for market in sample:
        cid = market["condition_id"]
        prices = market.get("outcome_prices") or []
        for group in get_market_holders(cid):
            for holder in group.get("holders", []):
                wallet = holder.get("proxyWallet")
                amount = holder.get("amount")
                if not wallet or amount is None:
                    continue
                try:
                    idx = int(holder.get("outcomeIndex"))
                except (TypeError, ValueError):
                    continue
                price = prices[idx] if 0 <= idx < len(prices) else None
                if price is None:
                    continue
                whale = acc.setdefault(wallet, Whale(proxy_wallet=wallet))
                whale.total_value_usd += float(amount) * price
                whale.markets.add(cid)

    ranked = sorted(acc.values(), key=lambda w: w.total_value_usd, reverse=True)
    logger.info("Rankeadas %d wallets sobre %d mercados (valor en $)", len(ranked), len(sample))
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


def politics_portfolio_share(
    positions: list[dict[str, Any]],
    politics_conditions: set[str],
) -> tuple[float, float]:
    """Valor en politica y valor total de una cartera (foto actual).

    Suma el `currentValue` (valor en $ a precio actual) de todas las posiciones y,
    aparte, el de las que estan en un mercado de politica (`conditionId` en
    `politics_conditions`). Sirve para filtrar/mostrar que ballenas estan de
    verdad en politica y cuales solo aparecen por posiciones de otras categorias.

    Returns:
        (valor_en_politica, valor_total). El share es valor_en_politica/valor_total.
    """
    total = 0.0
    politics = 0.0
    for pos in positions:
        try:
            value = float(pos.get("currentValue") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        total += value
        if pos.get("conditionId") in politics_conditions:
            politics += value
    return politics, total


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
    """Demo: rankea ballenas por valor en $ sobre los mercados de politica en vivo."""
    from src.client.gamma import flatten_markets, get_politics_events

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    markets = flatten_markets(get_politics_events())

    whales = rank_whales(markets, top_n=10)
    print(f"\nTop {len(whales)} ballenas por valor en $ (precio actual):\n")
    for w in whales:
        print(f"  ${w.total_value_usd:>14,.0f}  {w.proxy_wallet}  ({len(w.markets)} mercados)")


if __name__ == "__main__":
    _main()
