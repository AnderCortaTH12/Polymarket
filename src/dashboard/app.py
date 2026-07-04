"""Dashboard Streamlit de Polymarket Politics.

Tres secciones: vista general (tabla de mercados), detalle de mercado (grafica
historica de CLOB) y panel de control (sidebar con auto-refresco y estado del
collector). Los datos del API se cachean con TTL para no repetir llamadas.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

# `streamlit run` no pone la raiz del proyecto en sys.path; la añadimos para
# poder importar el paquete `src`.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.client.clob import get_price_history
from src.client.gamma import flatten_markets, get_politics_events
from src.collector.models import DB_PATH

logger = logging.getLogger(__name__)

st.set_page_config(page_title="Polymarket Politics", page_icon="🗳️", layout="wide")


# --------------------------------------------------------------------------- #
# Carga de datos (cacheada)
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=30)
def load_markets() -> list[dict[str, Any]]:
    """Descarga y aplana los mercados de politica. Cacheado 30s (Gamma cachea igual)."""
    return flatten_markets(get_politics_events())


@st.cache_data(ttl=30)
def load_price_history(token_id: str, interval: str) -> pd.DataFrame:
    """Serie historica de un outcome como DataFrame indexado por fecha."""
    history = get_price_history(token_id, interval=interval, fidelity=60)
    if not history:
        return pd.DataFrame(columns=["precio"])
    df = pd.DataFrame(history)
    df["fecha"] = pd.to_datetime(df["t"], unit="s")
    return df.set_index("fecha")[["p"]].rename(columns={"p": "precio"})


def collector_status() -> dict[str, Any]:
    """Estado de la BD del collector: existe, nº de snapshots y antiguedad del ultimo."""
    if not DB_PATH.exists():
        return {"exists": False}
    conn = sqlite3.connect(DB_PATH)
    try:
        n = conn.execute("SELECT COUNT(DISTINCT ts) FROM snapshots").fetchone()[0]
        last = conn.execute("SELECT MAX(ts) FROM snapshots").fetchone()[0]
    except sqlite3.Error:
        return {"exists": True, "snapshots": 0, "last": None}
    finally:
        conn.close()
    return {"exists": True, "snapshots": n, "last": last}


def _prob(row: dict[str, Any]) -> float | None:
    """Probabilidad del primer outcome (para la barra de progreso)."""
    prices = row.get("outcome_prices") or []
    return prices[0] if prices and prices[0] is not None else None


# --------------------------------------------------------------------------- #
# Sidebar — panel de control
# --------------------------------------------------------------------------- #
def render_sidebar() -> dict[str, Any]:
    """Dibuja el sidebar y devuelve la configuracion elegida por el usuario."""
    st.sidebar.header("⚙️ Panel de control")
    auto = st.sidebar.toggle("Auto-refresco", value=False)
    every = st.sidebar.slider("Refrescar cada (s)", 30, 300, 60, step=10)

    st.sidebar.divider()
    st.sidebar.subheader("Estado del collector")
    status = collector_status()
    if not status["exists"]:
        st.sidebar.info("No existe la base de datos del collector todavia.")
    else:
        st.sidebar.metric("Snapshots", status.get("snapshots", 0))
        last = status.get("last")
        if last:
            delta = datetime.now(timezone.utc) - datetime.fromisoformat(last)
            st.sidebar.caption(f"Ultimo snapshot hace {int(delta.total_seconds() // 60)} min")
    return {"auto": auto, "every": every}


# --------------------------------------------------------------------------- #
# Seccion 1 — vista general
# --------------------------------------------------------------------------- #
def render_overview(markets: list[dict[str, Any]]) -> None:
    """Tabla de mercados con metricas de cabecera, buscador y filtro de volumen."""
    st.subheader("📊 Vista general")

    total_vol = sum(m["volume_24h"] or 0 for m in markets)
    total_liq = sum(m["liquidity"] or 0 for m in markets)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Mercados", len(markets))
    c2.metric("Volumen 24h", f"${total_vol:,.0f}")
    c3.metric("Liquidez total", f"${total_liq:,.0f}")
    c4.metric("Actualizado", datetime.now().strftime("%H:%M:%S"))

    query = st.text_input("Buscar (pregunta o evento)", "")
    min_vol = st.slider("Volumen 24h minimo ($)", 0, 100_000, 0, step=1_000)

    rows = []
    for m in markets:
        if (m["volume_24h"] or 0) < min_vol:
            continue
        text = f"{m.get('question', '')} {m.get('event_title', '')}".lower()
        if query and query.lower() not in text:
            continue
        rows.append({
            "Mercado": m.get("question"),
            "Prob.": _prob(m),
            "Spread": m.get("spread"),
            "Volumen 24h": m.get("volume_24h"),
            "Liquidez": m.get("liquidity"),
            "Link": m.get("url"),
        })

    df = pd.DataFrame(rows).sort_values("Volumen 24h", ascending=False, na_position="last")
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "Prob.": st.column_config.ProgressColumn("Prob.", min_value=0, max_value=1, format="%.0f%%"),
            "Volumen 24h": st.column_config.NumberColumn(format="$%.0f"),
            "Liquidez": st.column_config.NumberColumn(format="$%.0f"),
            "Link": st.column_config.LinkColumn("Link", display_text="Abrir"),
        },
    )


# --------------------------------------------------------------------------- #
# Seccion 2 — detalle de mercado
# --------------------------------------------------------------------------- #
def render_detail(markets: list[dict[str, Any]]) -> None:
    """Detalle de un mercado: grafica historica de CLOB y metricas."""
    st.subheader("🔍 Detalle de mercado")

    tradeable = [m for m in markets if m.get("clob_token_ids")]
    if not tradeable:
        st.info("No hay mercados con token CLOB disponible.")
        return

    labels = {f"{m['question']}": m for m in tradeable}
    choice = st.selectbox("Mercado", list(labels.keys()))
    market = labels[choice]

    c1, c2, c3 = st.columns(3)
    prob = _prob(market)
    c1.metric("Probabilidad", f"{prob:.0%}" if prob is not None else "—")
    c2.metric("Volumen 24h", f"${market['volume_24h'] or 0:,.0f}")
    c3.metric("Liquidez", f"${market['liquidity'] or 0:,.0f}")

    outcomes = market.get("outcomes") or []
    tokens = market.get("clob_token_ids") or []
    idx = 0
    if len(outcomes) == len(tokens) and len(outcomes) > 1:
        outcome = st.radio("Outcome", outcomes, horizontal=True)
        idx = outcomes.index(outcome)
    interval = st.select_slider("Rango", options=["1d", "1w", "1m", "max"], value="1w")

    df = load_price_history(tokens[idx], interval)
    if df.empty:
        st.warning("Sin datos historicos para este outcome.")
    else:
        st.line_chart(df, y="precio")

    if market.get("url"):
        st.link_button("Ver en Polymarket", market["url"])


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    """Punto de entrada del dashboard."""
    st.title("🗳️ Polymarket Politics")
    cfg = render_sidebar()

    try:
        markets = load_markets()
    except Exception as exc:  # noqa: BLE001 - mostrar el fallo en la UI, no crashear
        st.error(f"Error cargando mercados: {exc}")
        return

    render_overview(markets)
    st.divider()
    render_detail(markets)

    if cfg["auto"]:
        time.sleep(cfg["every"])
        st.rerun()


if __name__ == "__main__":
    main()
