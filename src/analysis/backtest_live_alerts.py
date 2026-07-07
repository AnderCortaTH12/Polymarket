"""Paso 4 de la Fase 6: backtest cuantitativo sobre las alertas acumuladas.

Lee la tabla `alerts` (una fila por deteccion, con el score desglosado en JSON) y
simula que hubiera pasado si, en el momento de cada alerta, hubieramos entrado al
mercado en el mismo lado que el trader vigilado. Cada estrategia de salida
(`ExitStrategy`) define cuando y a que precio cerramos, y de ahi sale el P&L.

Diseño para testear sin red: toda la logica de agregacion es pura. La unica
parte que toca la red (obtener el precio de salida real) esta aislada en un
"resolver" inyectable (`resolve_exit`); los tests pasan un resolver falso. El
resolver por defecto (`LivePriceResolver`) usa Gamma (precio final si el mercado
esta resuelto) y CLOB (serie historica para las salidas por horizonte temporal).

Honestidad: con pocas alertas los numeros no son concluyentes. El backtest
reporta lo que hay; no ajusta nada para "hacer que salga bien".
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable

import pandas as pd

from src import config
from src.analysis.pnl import compute_pnl
from src.analysis.scoring import ScoreBreakdown

logger = logging.getLogger(__name__)

# Horizontes temporales en horas para las estrategias HORIZON_*.
HORIZON_HOURS: dict[str, int] = {
    "HORIZON_24H": 24,
    "HORIZON_72H": 72,
    "HORIZON_1W": 168,
}


class ExitStrategy(Enum):
    """Cuando cerramos la posicion simulada abierta al detectar la alerta."""

    RESOLUTION = "resolution"                    # mantener hasta que el mercado cierre (payout 0/1)
    HORIZON_24H = "horizon_24h"                   # vender al precio 24h despues
    HORIZON_72H = "horizon_72h"                   # vender al precio 72h despues
    HORIZON_1W = "horizon_1w"                     # vender al precio 1 semana despues
    TAKE_PROFIT_15_STOP_10 = "tp15_stop10"       # salir si sube 15% o baja 10%
    SIGNAL_REVERSAL = "signal_reversal"          # salir si el score cae bajo el umbral


# Componentes del score (campos int de ScoreBreakdown que suman puntos).
SCORE_COMPONENTS: tuple[str, ...] = (
    "wallet_fresca",
    "sin_perfil",
    "tamano_anomalo",
    "longshot",
    "track_record",
    "cluster",
    "concentracion",
    "flujo_toxico",
    "insensibilidad_precio",
)


@dataclass
class SimulatedTrade:
    """Una posicion simulada abierta en el momento de una alerta.

    Entramos en el mismo `side` (outcome Yes/No) y `trade_side` (BUY/SELL) que el
    trader vigilado, con `size_usd` de capital nominal.
    """

    alert_id: int
    entry_price: float
    size_usd: float
    condition_id: str
    side: str          # outcome operado (Yes/No)
    trade_side: str    # BUY / SELL
    score: int
    ts_entry: str
    breakdown: dict[str, Any] = field(default_factory=dict)

    @property
    def shares(self) -> float:
        """Numero de shares del outcome que compra `size_usd` al precio de entrada."""
        if not self.entry_price:
            return 0.0
        return self.size_usd / self.entry_price


@dataclass
class ExitObservation:
    """Resultado de resolver la salida de una posicion segun una estrategia."""

    exit_price: float | None
    status: str  # "resolved" | "active" | "na"
    ts_exit: str | None = None


# Un resolver toma la posicion y la estrategia y devuelve donde/como salimos.
ExitResolver = Callable[[SimulatedTrade, ExitStrategy], ExitObservation]


def _parse_breakdown(raw: Any) -> dict[str, Any]:
    """Parsea el JSON de `score_breakdown`; dict vacio si no se puede."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _row_to_trade(row: Any) -> SimulatedTrade:
    """Convierte una fila de la tabla `alerts` en una `SimulatedTrade`."""
    breakdown = _parse_breakdown(row.get("score_breakdown"))
    return SimulatedTrade(
        alert_id=int(row.get("id") if row.get("id") is not None else row.get("alert_id", 0)),
        entry_price=float(row.get("price_at_detection") or 0.0),
        size_usd=float(row.get("trade_size_usd") or 0.0),
        condition_id=str(row.get("condition_id") or ""),
        side=str(row.get("side") or ""),
        trade_side=str(row.get("trade_side") or "BUY"),
        score=int(row.get("score_total") or 0),
        ts_entry=str(row.get("ts") or ""),
        breakdown=breakdown,
    )


def execute_backtest(
    alerts_df: pd.DataFrame,
    exit_strategy: ExitStrategy,
    resolve_exit: ExitResolver,
    lookback_hours: int = 168,
) -> pd.DataFrame:
    """Simula entrar en cada alerta y cerrar segun `exit_strategy`.

    Args:
        alerts_df: alertas de la BD (columnas de la tabla `alerts`).
        exit_strategy: cuando/como cerramos cada posicion.
        resolve_exit: funcion (inyectable) que devuelve el precio de salida real.
            Aislar aqui la red permite testear la agregacion sin llamadas externas.
        lookback_hours: ventana maxima; alertas mas antiguas que esto se pueden
            cerrar por horizonte, las mas recientes que el horizonte quedan "na".

    Returns:
        DataFrame con [alert_id, entry_price, exit_price, pnl, pnl_pct, score,
        status, component_breakdown]. Las posiciones sin precio de salida
        (status "na") van con pnl NaN y no cuentan en la agregacion.
    """
    records: list[dict[str, Any]] = []
    for _, row in alerts_df.iterrows():
        trade = _row_to_trade(dict(row))
        if trade.entry_price <= 0:
            records.append(_na_record(trade, "entry invalido"))
            continue
        obs = resolve_exit(trade, exit_strategy)
        if obs.status == "na" or obs.exit_price is None:
            records.append(_na_record(trade, obs.status))
            continue
        pnl, pnl_pct = compute_pnl(trade.entry_price, trade.size_usd, trade.trade_side, float(obs.exit_price))
        records.append({
            "alert_id": trade.alert_id,
            "entry_price": trade.entry_price,
            "exit_price": float(obs.exit_price),
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "score": trade.score,
            "status": obs.status,
            "component_breakdown": trade.breakdown,
        })
    return pd.DataFrame.from_records(records)


def _na_record(trade: SimulatedTrade, status: str) -> dict[str, Any]:
    """Fila de un trade que no se pudo valorar (sin precio de salida)."""
    return {
        "alert_id": trade.alert_id,
        "entry_price": trade.entry_price,
        "exit_price": None,
        "pnl": float("nan"),
        "pnl_pct": float("nan"),
        "score": trade.score,
        "status": "na",
        "component_breakdown": trade.breakdown,
    }


def _max_drawdown(equity_curve: list[float]) -> float:
    """Maximo drawdown (fraccion, 0..1) de una curva de capital acumulada."""
    peak = -math.inf
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            dd = (peak - value) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def _equity_curve(returns_frac: list[float]) -> list[float]:
    """Curva de capital: producto acumulado de (1+retorno), empezando en 1.0."""
    curve: list[float] = []
    capital = 1.0
    for r in returns_frac:
        capital *= (1.0 + r)
        curve.append(capital)
    return curve


def aggregate_results(backtest_df: pd.DataFrame) -> dict[str, Any]:
    """Metricas agregadas de un backtest (ignora filas 'na' sin P&L).

    Returns dict con: n_trades, retorno_medio_pct, retorno_mediano_pct,
    pct_rentables, max_ganancia_pct, max_perdida_pct, max_drawdown, sharpe,
    equity_curve (lista de capital acumulado, base 1.0).
    """
    valid = backtest_df[backtest_df["pnl"].notna()] if not backtest_df.empty else backtest_df
    if valid.empty:
        return {
            "n_trades": 0,
            "retorno_medio_pct": 0.0,
            "retorno_mediano_pct": 0.0,
            "pct_rentables": 0.0,
            "max_ganancia_pct": 0.0,
            "max_perdida_pct": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "equity_curve": [],
        }

    pct = valid["pnl_pct"].astype(float)
    returns_frac = (pct / 100.0).tolist()
    curve = _equity_curve(returns_frac)
    mean_r = float(pct.mean())
    std_r = float(pct.std(ddof=0))
    sharpe = (mean_r / std_r) if std_r > 0 else 0.0
    return {
        "n_trades": int(len(valid)),
        "retorno_medio_pct": mean_r,
        "retorno_mediano_pct": float(pct.median()),
        "pct_rentables": float((valid["pnl"] > 0).mean() * 100.0),
        "max_ganancia_pct": float(pct.max()),
        "max_perdida_pct": float(pct.min()),
        "max_drawdown": _max_drawdown(curve),
        "sharpe": sharpe,
        "equity_curve": curve,
    }


def compare_strategies(
    alerts_df: pd.DataFrame,
    resolve_exit: ExitResolver,
    strategies: tuple[ExitStrategy, ...] | None = None,
) -> pd.DataFrame:
    """Ejecuta el backtest para cada estrategia y devuelve una tabla comparativa.

    Columnas: [estrategia, n_trades, retorno_medio, pct_rentables, sharpe,
    max_drawdown]. Ordenada por retorno_medio descendente.
    """
    strategies = strategies or (ExitStrategy.RESOLUTION, ExitStrategy.HORIZON_24H, ExitStrategy.HORIZON_1W)
    rows: list[dict[str, Any]] = []
    for strat in strategies:
        bt = execute_backtest(alerts_df, strat, resolve_exit)
        agg = aggregate_results(bt)
        rows.append({
            "estrategia": strat.value,
            "n_trades": agg["n_trades"],
            "retorno_medio": agg["retorno_medio_pct"],
            "pct_rentables": agg["pct_rentables"],
            "sharpe": agg["sharpe"],
            "max_drawdown": agg["max_drawdown"],
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("retorno_medio", ascending=False).reset_index(drop=True)
    return df


def analyze_by_component(
    alerts_df: pd.DataFrame,
    resolve_exit: ExitResolver,
    exit_strategy: ExitStrategy = ExitStrategy.RESOLUTION,
) -> pd.DataFrame:
    """Retorno medio de las alertas en las que CADA componente puntuo.

    Responde: ¿que señales tienen valor predictivo por si solas? Para cada
    componente del score, filtra las alertas donde contribuyo (> 0 puntos) y
    calcula su retorno medio y % de rentables.

    Columnas: [componente, n_alertas, retorno_medio, pct_rentables].
    """
    bt = execute_backtest(alerts_df, exit_strategy, resolve_exit)
    valid = bt[bt["pnl"].notna()] if not bt.empty else bt
    rows: list[dict[str, Any]] = []
    for comp in SCORE_COMPONENTS:
        mask = valid["component_breakdown"].apply(lambda d, c=comp: float(d.get(c, 0) or 0) > 0) if not valid.empty else []
        sub = valid[mask] if len(valid) else valid
        if sub.empty:
            rows.append({"componente": comp, "n_alertas": 0, "retorno_medio": 0.0, "pct_rentables": 0.0})
            continue
        rows.append({
            "componente": comp,
            "n_alertas": int(len(sub)),
            "retorno_medio": float(sub["pnl_pct"].mean()),
            "pct_rentables": float((sub["pnl"] > 0).mean() * 100.0),
        })
    return pd.DataFrame(rows).sort_values("retorno_medio", ascending=False).reset_index(drop=True)


def _longshot_tier_label(breakdown: dict[str, Any]) -> str:
    """Etiqueta del tramo de longshot de una alerta a partir de su breakdown."""
    tier = breakdown.get("longshot_tier")
    if tier is None:
        return "sin longshot"
    return f"<= {float(tier):.2f}"


def analyze_by_longshot_tier(
    alerts_df: pd.DataFrame,
    resolve_exit: ExitResolver,
    exit_strategy: ExitStrategy = ExitStrategy.RESOLUTION,
) -> pd.DataFrame:
    """Retorno medio agrupado por tramo de longshot (0.10 / 0.20 / 0.35 / ninguno).

    Responde: ¿los precios mas extremos (<0.10) predicen mejor?
    Columnas: [tramo, n_alertas, retorno_medio, pct_rentables].
    """
    bt = execute_backtest(alerts_df, exit_strategy, resolve_exit)
    valid = bt[bt["pnl"].notna()].copy() if not bt.empty else bt
    if valid.empty:
        return pd.DataFrame(columns=["tramo", "n_alertas", "retorno_medio", "pct_rentables"])
    valid["tramo"] = valid["component_breakdown"].apply(_longshot_tier_label)
    rows: list[dict[str, Any]] = []
    for tramo, sub in valid.groupby("tramo"):
        rows.append({
            "tramo": tramo,
            "n_alertas": int(len(sub)),
            "retorno_medio": float(sub["pnl_pct"].mean()),
            "pct_rentables": float((sub["pnl"] > 0).mean() * 100.0),
        })
    return pd.DataFrame(rows).sort_values("tramo").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Resolver en vivo (la unica parte que toca la red). Aislado para testear.
# ---------------------------------------------------------------------------


def _parse_ts(ts: str) -> datetime | None:
    """Parsea el timestamp ISO de una alerta a datetime UTC (None si falla)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class LivePriceResolver:
    """Resolver de salida que consulta Gamma (resueltos) y CLOB (horizontes).

    Cachea por condition_id el mercado de Gamma (para mapear el outcome `side` a
    su CLOB token id y saber si esta resuelto). Best-effort: si falta un dato
    devuelve status "na" y esa alerta no cuenta (mejor que inventar un precio).
    """

    def __init__(self) -> None:
        self._market_cache: dict[str, dict[str, Any] | None] = {}

    def _market(self, condition_id: str) -> dict[str, Any] | None:
        if condition_id not in self._market_cache:
            from src.client.gamma import get_market_by_condition
            try:
                self._market_cache[condition_id] = get_market_by_condition(condition_id)
            except Exception as exc:  # noqa: BLE001 - best effort, red inestable
                logger.warning("Gamma fallo para %s: %s", condition_id, exc)
                self._market_cache[condition_id] = None
        return self._market_cache[condition_id]

    def _token_and_index(self, market: dict[str, Any], side: str) -> tuple[str | None, int | None]:
        """CLOB token id del outcome `side` (Yes/No) y su indice."""
        from src.client.gamma import _parse_json_field
        outcomes = _parse_json_field(market.get("outcomes")) or []
        tokens = _parse_json_field(market.get("clobTokenIds")) or []
        for i, name in enumerate(outcomes):
            if str(name).strip().lower() == side.strip().lower():
                token = tokens[i] if i < len(tokens) else None
                return (str(token) if token else None), i
        return None, None

    def __call__(self, trade: SimulatedTrade, strategy: ExitStrategy) -> ExitObservation:
        market = self._market(trade.condition_id)
        if market is None:
            return ExitObservation(None, "na")

        from src.client.gamma import _is_truthy, _parse_json_field, _to_float
        closed = _is_truthy(market.get("closed"))

        if strategy == ExitStrategy.RESOLUTION:
            if not closed:
                return ExitObservation(None, "na")  # aun no resuelto: no valorable
            _, idx = self._token_and_index(market, trade.side)
            prices = _parse_json_field(market.get("outcomePrices")) or []
            if idx is None or idx >= len(prices):
                return ExitObservation(None, "na")
            final = _to_float(prices[idx])
            return ExitObservation(final, "resolved")

        # Estrategias por horizonte temporal: precio del outcome N horas despues.
        hours = HORIZON_HOURS.get(strategy.name)
        if hours is not None:
            return self._horizon_exit(trade, market, hours)

        if strategy == ExitStrategy.TAKE_PROFIT_15_STOP_10:
            return self._tp_stop_exit(trade, market)

        # SIGNAL_REVERSAL requiere re-scorear con el contexto de cubos/perfil de
        # ese instante, que no se reconstruye de forma fiable offline. Honesto:
        # no lo valoramos en vivo (los tests lo cubren con resolver falso).
        return ExitObservation(None, "na")

    def _price_series(self, market: dict[str, Any], side: str) -> list[dict[str, Any]]:
        from src.client.clob import get_price_history
        token, _ = self._token_and_index(market, side)
        if not token:
            return []
        try:
            return get_price_history(token, interval="max", fidelity=60)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CLOB fallo para token %s: %s", token, exc)
            return []

    def _horizon_exit(self, trade: SimulatedTrade, market: dict[str, Any], hours: int) -> ExitObservation:
        entry_dt = _parse_ts(trade.ts_entry)
        if entry_dt is None:
            return ExitObservation(None, "na")
        target = entry_dt + timedelta(hours=hours)
        if target > datetime.now(timezone.utc):
            return ExitObservation(None, "na")  # el horizonte aun no ha ocurrido
        series = self._price_series(market, trade.side)
        price = _price_at(series, target)
        if price is None:
            return ExitObservation(None, "na")
        return ExitObservation(price, "active", ts_exit=target.isoformat())

    def _tp_stop_exit(self, trade: SimulatedTrade, market: dict[str, Any]) -> ExitObservation:
        entry_dt = _parse_ts(trade.ts_entry)
        if entry_dt is None:
            return ExitObservation(None, "na")
        series = self._price_series(market, trade.side)
        if not series:
            return ExitObservation(None, "na")
        tp = trade.entry_price * 1.15
        stop = trade.entry_price * 0.90
        for pt in series:
            t = pt.get("t")
            if t is None or datetime.fromtimestamp(float(t), timezone.utc) < entry_dt:
                continue
            p = _to_float_safe(pt.get("p"))
            if p is None:
                continue
            if p >= tp or p <= stop:
                return ExitObservation(p, "active", ts_exit=datetime.fromtimestamp(float(t), timezone.utc).isoformat())
        # Nunca cruzo: salimos al ultimo precio disponible.
        last = _to_float_safe(series[-1].get("p")) if series else None
        return ExitObservation(last, "active") if last is not None else ExitObservation(None, "na")


def _to_float_safe(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price_at(series: list[dict[str, Any]], target: datetime) -> float | None:
    """Precio de la serie en el punto mas cercano <= target (ultimo conocido)."""
    if not series:
        return None
    target_ts = target.timestamp()
    best: float | None = None
    for pt in series:
        t = pt.get("t")
        if t is None:
            continue
        if float(t) <= target_ts:
            p = _to_float_safe(pt.get("p"))
            if p is not None:
                best = p
        else:
            break
    return best
