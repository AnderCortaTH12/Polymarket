"""Punto de entrada del backtest cuantitativo (Paso 4 de la Fase 6).

Lee TODAS las alertas de la BD (`alerts`), compara estrategias de salida y
analiza la predictividad por componente y por tramo de longshot. Genera un
informe Markdown en `reports/backtest_FECHA.md` y un resumen ejecutivo en
pantalla.

Uso (tras acumular alertas con el detector corriendo):
    python -m src.backtest_runner
    python -m src.backtest_runner --min-score 50

Es honesto: si con las alertas disponibles no hay señal, lo dice. No ajusta
pesos para forzar un resultado.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from src.analysis import backtest_live_alerts as bt
from src.analysis.backtest_live_alerts import ExitStrategy, LivePriceResolver
from src.collector.models import DB_PATH
from src.realtime import storage

logger = logging.getLogger(__name__)

# La consola de Windows es cp1252 y peta con algunos caracteres: forzamos UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass

REPORTS_DIR: Path = Path(__file__).resolve().parents[1] / "reports"

MAIN_STRATEGIES: tuple[ExitStrategy, ...] = (
    ExitStrategy.RESOLUTION,
    ExitStrategy.HORIZON_24H,
    ExitStrategy.HORIZON_1W,
)


def load_alerts(min_score: int = 0, scoring_version: str | None = "v3") -> tuple[pd.DataFrame, int]:
    """Carga las alertas de la BD como DataFrame, filtrando por version del scoring.

    Por defecto solo devuelve las alertas del scoring vigente (v3). Las v1 se
    puntuaron con el perfilado inactivo y las v2 con track_record activo sobre un
    win_rate falso (ver Fase 1c); mezclar cualquiera de ellas invalidaria el
    backtest. Devuelve (df, n_legacy_excluidas) para poder avisar por pantalla.
    """
    conn = storage.connect(DB_PATH)
    try:
        base = (
            "SELECT id, ts, condition_id, market_question, wallet, side, trade_side, "
            "trade_size_usd, price_at_detection, score_total, score_breakdown, bucket_imbalance "
            "FROM alerts WHERE score_total >= ?"
        )
        params: tuple[Any, ...] = (min_score,)
        if scoring_version is not None:
            # NULL = alertas anteriores a la migracion; se tratan como v1.
            base += " AND COALESCE(scoring_version, 'v1') = ?"
            params = (min_score, scoring_version)
        df = pd.read_sql_query(base + " ORDER BY ts ASC", conn, params=params)
        excluded = 0
        if scoring_version is not None:
            row = conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE score_total >= ? "
                "AND COALESCE(scoring_version, 'v1') != ?",
                (min_score, scoring_version),
            ).fetchone()
            excluded = int(row[0]) if row else 0
        return df, excluded
    finally:
        conn.close()


def _ascii_equity_curve(curve: list[float], width: int = 50, height: int = 8) -> str:
    """Grafica ASCII simple de la curva de capital (base 1.0)."""
    if not curve:
        return "(sin datos)"
    lo, hi = min(curve), max(curve)
    span = (hi - lo) or 1.0
    # Remuestrea a `width` columnas.
    n = len(curve)
    cols = [curve[round(i * (n - 1) / (width - 1))] for i in range(min(width, n))] if n > 1 else curve
    grid = [[" "] * len(cols) for _ in range(height)]
    for x, v in enumerate(cols):
        y = height - 1 - round((v - lo) / span * (height - 1))
        grid[y][x] = "*"
    body = "\n".join("".join(r) for r in grid)
    return f"cap max {hi:.3f}\n{body}\ncap min {lo:.3f}"


def _df_to_md(df: pd.DataFrame) -> str:
    """Tabla Markdown de un DataFrame (redondeando floats). Sin dependencias."""
    if df.empty:
        return "_(sin filas)_"
    rounded = df.copy()
    for col in rounded.select_dtypes(include="float").columns:
        rounded[col] = rounded[col].round(2)
    cols = [str(c) for c in rounded.columns]
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = "\n".join(
        "| " + " | ".join(str(v) for v in row) + " |"
        for row in rounded.itertuples(index=False, name=None)
    )
    return "\n".join([header, sep, body])


def build_report(alerts_df: pd.DataFrame, resolve_exit: bt.ExitResolver) -> tuple[str, dict[str, object]]:
    """Construye el informe Markdown y devuelve (texto, resumen_ejecutivo)."""
    n = len(alerts_df)
    comparison = bt.compare_strategies(alerts_df, resolve_exit, MAIN_STRATEGIES)
    by_component = bt.analyze_by_component(alerts_df, resolve_exit)
    by_tier = bt.analyze_by_longshot_tier(alerts_df, resolve_exit)

    # Curva de capital de la mejor estrategia por retorno medio.
    best_curve: list[float] = []
    best_strat = "-"
    if not comparison.empty:
        best_strat = str(comparison.iloc[0]["estrategia"])
        strat_enum = next((s for s in MAIN_STRATEGIES if s.value == best_strat), MAIN_STRATEGIES[0])
        agg = bt.aggregate_results(bt.execute_backtest(alerts_df, strat_enum, resolve_exit))
        best_curve = agg["equity_curve"]

    now = datetime.now(timezone.utc)
    caveat = ""
    if n < 50:
        caveat = (
            f"\n> **Aviso**: solo hay {n} alertas. Los resultados NO son "
            "concluyentes; sirven para validar la maquinaria. Repite el backtest "
            "con >=50 alertas para sacar conclusiones sobre los pesos.\n"
        )

    lines = [
        f"# Backtest de alertas — {now:%Y-%m-%d %H:%M UTC}",
        "",
        f"Alertas analizadas: **{n}**.",
        caveat,
        "## Estrategias de salida comparadas",
        "",
        _df_to_md(comparison),
        "",
        f"Mejor por retorno medio: **{best_strat}**.",
        "",
        "### Curva de capital (mejor estrategia, base 1.0)",
        "",
        "```",
        _ascii_equity_curve(best_curve),
        "```",
        "",
        "## Predictividad por componente del score",
        "",
        "Retorno medio de las alertas en las que cada componente puntuo "
        "(salida por resolucion). Un componente con muchas alertas y buen "
        "retorno es un buen predictor; uno con retorno negativo resta.",
        "",
        _df_to_md(by_component),
        "",
        "## Retorno por tramo de longshot",
        "",
        "¿Los precios mas extremos (<0.10) predicen mejor?",
        "",
        _df_to_md(by_tier),
        "",
        "## Conclusiones",
        "",
        _conclusions(comparison, by_component, by_tier, n),
        "",
    ]
    report = "\n".join(lines)
    summary = {
        "n": n,
        "best_strat": best_strat,
        "comparison": comparison,
    }
    return report, summary


def _conclusions(comparison: pd.DataFrame, by_component: pd.DataFrame, by_tier: pd.DataFrame, n: int) -> str:
    """Redacta conclusiones honestas a partir de las tablas."""
    if comparison.empty or comparison["n_trades"].sum() == 0:
        return (
            "No hay trades valorables todavia (los mercados de las alertas siguen "
            "abiertos o falta historial de precio). Vuelve a ejecutar cuando "
            "algunos mercados se hayan resuelto."
        )
    parts: list[str] = []
    best = comparison.iloc[0]
    parts.append(
        f"- La estrategia **{best['estrategia']}** da el mejor retorno medio "
        f"({best['retorno_medio']:.1f}%), con {best['pct_rentables']:.0f}% de trades "
        f"rentables y Sharpe {best['sharpe']:.2f}."
    )
    strong = by_component[(by_component["n_alertas"] > 0) & (by_component["retorno_medio"] > 0)]
    weak = by_component[(by_component["n_alertas"] > 0) & (by_component["retorno_medio"] < 0)]
    if not strong.empty:
        parts.append("- Componentes con retorno positivo (candidatos a MANTENER/subir peso): "
                     + ", ".join(f"{r['componente']} ({r['retorno_medio']:.0f}%)" for _, r in strong.iterrows()) + ".")
    if not weak.empty:
        parts.append("- Componentes con retorno negativo (candidatos a REVISAR/bajar peso): "
                     + ", ".join(f"{r['componente']} ({r['retorno_medio']:.0f}%)" for _, r in weak.iterrows()) + ".")
    if not by_tier.empty:
        best_tier = by_tier.sort_values("retorno_medio", ascending=False).iloc[0]
        parts.append(f"- El tramo de longshot mas rentable es **{best_tier['tramo']}** "
                     f"({best_tier['retorno_medio']:.0f}%).")
    if n < 50:
        parts.append("- **Muestra pequeña**: trata todo lo anterior como indicativo, no como veredicto.")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest cuantitativo de las alertas del detector.")
    parser.add_argument("--min-score", type=int, default=0, help="Solo alertas con score_total >= este valor.")
    parser.add_argument(
        "--include-legacy", "--include-v1", dest="include_legacy", action="store_true",
        help="Incluir las alertas v1/v2 (scorings antiguos). Por defecto se excluyen.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    scoring_version = None if args.include_legacy else "v3"
    alerts, excluded_legacy = load_alerts(args.min_score, scoring_version)
    if excluded_legacy:
        print(
            f"[aviso] Excluidas {excluded_legacy} alertas v1/v2: se puntuaron con "
            "scorings antiguos (v1 perfilado inactivo, v2 track_record sobre un "
            "win_rate falso) y contaminarian el backtest. Usa --include-legacy "
            "para incluirlas (no recomendado)."
        )
    if alerts.empty:
        print("No hay alertas en la BD (o ninguna supera --min-score). Nada que backtestear.")
        return 0

    print(f"Cargadas {len(alerts)} alertas. Resolviendo precios de salida (puede tardar por la red)...")
    resolver = LivePriceResolver()
    report, summary = build_report(alerts, resolver)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"backtest_{datetime.now(timezone.utc):%Y%m%d_%H%M}.md"
    out.write_text(report, encoding="utf-8")

    print("\n=== RESUMEN EJECUTIVO ===")
    print(f"Alertas: {summary['n']}  |  Mejor estrategia: {summary['best_strat']}")
    comp = summary["comparison"]
    if isinstance(comp, pd.DataFrame) and not comp.empty:
        print(comp.round(2).to_string(index=False))
    print(f"\nInforme completo: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
