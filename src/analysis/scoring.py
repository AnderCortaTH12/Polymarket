"""Paso 3a de la Fase 6: scoring compuesto de un trade (funcion pura).

`compute_score` recibe un trade, el perfil precomputado de la wallet (o None si
no se ha visto nunca), el imbalance del cubo de volumen actual del mercado y un
diccionario de contexto, y devuelve un `ScoreBreakdown` con los puntos de cada
componente y el total. NO hace llamadas a BD ni a la red: toda la orquestacion
(leer perfiles, mantener estado de cubos) vive fuera, en la capa rapida.

Lenguaje honesto: no "detectamos insiders", detectamos ANOMALIAS compatibles con
trading informado. El backtest dira que combinaciones tienen valor predictivo.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from src import config
from src.analysis.buckets import _trade_sign, _trade_usd


@dataclass
class ScoreBreakdown:
    """Puntos por componente del score de un trade, mas el total."""

    wallet_fresca: int = 0
    sin_perfil: int = 0
    tamano_anomalo: int = 0
    longshot: int = 0
    track_record: int = 0
    cluster: int = 0
    concentracion: int = 0
    flujo_toxico: int = 0
    insensibilidad_precio: int = 0
    score_total: int = 0

    def components(self) -> dict[str, int]:
        """Componentes (sin el total) para guardar como JSON en la alerta."""
        d = asdict(self)
        d.pop("score_total")
        return d


def compute_score(
    trade: dict[str, Any],
    wallet_profile: Any | None,
    bucket_imbalance: float,
    market_data: dict[str, Any] | None = None,
) -> ScoreBreakdown:
    """Puntua un trade sumando componentes de anomalia. Funcion pura.

    Args:
        trade: dict con side, outcome, size, price (precio del outcome operado)
            y conditionId.
        wallet_profile: `WalletProfile` de la wallet, o None si nunca se ha visto.
        bucket_imbalance: imbalance con signo del cubo de volumen actual (-1..+1,
            espacio Yes).
        market_data: contexto adicional que la capa rapida conoce:
            - "shared_cluster_ids": set de funders con >1 wallet (para `cluster`)
            - "same_side_streak": nº de trades seguidos de la wallet al mismo lado
            - "price_against": bool, si el precio se movio en su contra
    """
    w = config.SCORE_WEIGHTS
    md = market_data or {}
    b = ScoreBreakdown()

    trade_usd = _trade_usd(trade)
    side = str(trade.get("side", "")).strip().upper()
    try:
        price = float(trade.get("price"))
    except (TypeError, ValueError):
        price = None

    # --- Wallet fresca (y extra si es muy fresca) ---------------------------
    age = getattr(wallet_profile, "wallet_age_days", None) if wallet_profile else None
    if age is not None and age < config.FRESH_WALLET_DAYS:
        b.wallet_fresca = w["wallet_fresca"]
        if age < config.VERY_FRESH_WALLET_DAYS:
            b.wallet_fresca += w["wallet_fresca_extra"]

    # --- Sin perfil (nunca vista) + trade relevante -------------------------
    if wallet_profile is None and trade_usd > config.NO_PROFILE_MIN_TRADE_USD:
        b.sin_perfil = w["sin_perfil"]

    # --- Tamaño absoluto anomalo -------------------------------------------
    if trade_usd > config.BIG_TRADE_USD:
        b.tamano_anomalo = w["tamano_anomalo"]

    # --- Longshot con conviccion (comprar un outcome improbable) ------------
    if (
        side == "BUY"
        and price is not None
        and price < config.LONGSHOT_MAX_PROB
        and trade_usd > config.LONGSHOT_MIN_TRADE_USD
    ):
        b.longshot = w["longshot"]

    # --- Track record sospechoso -------------------------------------------
    win_rate = getattr(wallet_profile, "win_rate", None) if wallet_profile else None
    n_resolved = getattr(wallet_profile, "n_resolved", 0) if wallet_profile else 0
    if (
        win_rate is not None
        and win_rate > config.SUSPICIOUS_WIN_RATE
        and n_resolved >= config.MIN_RESOLVED_FOR_WINRATE
    ):
        b.track_record = w["track_record"]

    # --- Cluster de funding compartido -------------------------------------
    cluster_id = getattr(wallet_profile, "funding_cluster_id", None) if wallet_profile else None
    if cluster_id and cluster_id in md.get("shared_cluster_ids", set()):
        b.cluster = w["cluster"]

    # --- Concentracion (opera casi solo en un mercado) ----------------------
    concentration = getattr(wallet_profile, "concentration", 0.0) if wallet_profile else 0.0
    total_volume = getattr(wallet_profile, "total_volume_usd", 0.0) if wallet_profile else 0.0
    if concentration > config.CONCENTRATION_MIN and total_volume > config.CONCENTRATION_MIN_VOLUME_USD:
        b.concentracion = w["concentracion"]

    # --- Flujo toxico: el cubo empuja fuerte en la MISMA direccion del trade -
    trade_sign = _trade_sign(trade)
    if (
        abs(bucket_imbalance) > config.TOXIC_IMBALANCE
        and trade_sign != 0
        and (bucket_imbalance > 0) == (trade_sign > 0)
    ):
        b.flujo_toxico = w["flujo_toxico"]

    # --- Insensibilidad al precio (acumula el mismo lado a peor precio) ------
    if md.get("same_side_streak", 0) >= config.INSENSIBILITY_MIN_STREAK and md.get("price_against", False):
        b.insensibilidad_precio = w["insensibilidad_precio"]

    b.score_total = (
        b.wallet_fresca + b.sin_perfil + b.tamano_anomalo + b.longshot
        + b.track_record + b.cluster + b.concentracion + b.flujo_toxico
        + b.insensibilidad_precio
    )
    return b
