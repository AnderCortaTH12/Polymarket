"""Dashboard Streamlit de Polymarket Politics.

Tres secciones: vista general (tabla de mercados), detalle de mercado (grafica
historica de CLOB) y panel de control (sidebar con auto-refresco y estado del
collector). Los datos del API se cachean con TTL para no repetir llamadas.
"""
from __future__ import annotations

import json
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

from src import db
from src.analysis.pnl import format_pnl_cell
from src.analysis.whales import Whale, politics_portfolio_share, rank_whales
from src.client.clob import get_price_history
from src.client.data_api import get_user_positions
from src.client.gamma import (
    _is_truthy,
    _parse_json_field,
    _to_float,
    flatten_markets,
    get_market_by_condition,
    get_politics_events,
)
from src.collector.models import DB_PATH
from src.realtime import storage

# Cuantos mercados (por volumen) se agregan para rankear ballenas. Limitar
# mantiene el numero de llamadas a la Data API razonable.
WHALE_MARKETS_SAMPLE: int = 60
WHALE_TOP_N: int = 50
# Tope de posiciones a traer por ballena. Las grandes tienen miles; el endpoint
# las ordena por valor descendente, asi que el top concentra casi todo el valor
# y se carga en una sola llamada (evita esperas de minutos).
MAX_WHALE_POSITIONS: int = 300
# Alertas del detector (Fase 6).
ALERTS_LIMIT: int = 100
DETECTOR_DOWN_MINUTES: float = 4.0  # sin heartbeat mas reciente => detector caido
POLYMARKET_EVENT_URL: str = "https://polymarket.com/event"
POLYGONSCAN_TX_URL: str = "https://polygonscan.com/tx"

logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Polymarket Politics",
    page_icon=":material/insights:",  # icono monocromo Material, no emoji decorativo
    layout="wide",
)

# Retoques de tipografia y espaciado para una estetica mas cuidada y propia.
_CUSTOM_CSS = """
<style>
  .block-container { padding-top: 2.2rem; max-width: 1200px; }
  h1 { font-weight: 700; letter-spacing: -0.02em; }
  h2, h3 { font-weight: 600; letter-spacing: -0.01em; }
  /* Regla fina bajo la cabecera */
  .app-header { border-bottom: 1px solid #E5E1D8; padding-bottom: .6rem; margin-bottom: 1.2rem; }
  .app-header .subtitle { color: #6B6659; font-size: .95rem; margin-top: .1rem; }
  /* Pestañas mas sobrias */
  button[data-baseweb="tab"] { font-weight: 600; }
  [data-testid="stMetricValue"] { font-weight: 700; }
</style>
"""
st.markdown(_CUSTOM_CSS, unsafe_allow_html=True)


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


@st.cache_data(ttl=300)
def load_whales(top_n: int = WHALE_TOP_N) -> list[Whale]:
    """Rankea las top ballenas por VALOR EN $ sobre los mercados de politica.

    Cacheado 5 min. Usa `load_markets()` (ya cacheado) para tener los precios
    actuales con los que valorar las posiciones.
    """
    return rank_whales(load_markets(), top_n=top_n, max_markets=WHALE_MARKETS_SAMPLE)


@st.cache_data(ttl=300)
def load_user_positions(wallet: str) -> pd.DataFrame:
    """Top posiciones (por valor) de una wallet como DataFrame. Cacheado 5 min.

    Trae como mucho `MAX_WHALE_POSITIONS`, ordenadas por valor actual desc en el
    servidor: una sola llamada rapida. Suficiente para el % de cartera porque la
    cola (miles de posiciones minusculas de las ballenas grandes) no mueve el
    total y solo hacia la carga insoportablemente lenta.
    """
    positions = get_user_positions(wallet, max_positions=MAX_WHALE_POSITIONS)
    if not positions:
        return pd.DataFrame()
    return pd.DataFrame(positions)


@st.cache_data(ttl=30)
def load_alerts(min_score: int, limit: int) -> pd.DataFrame:
    """Ultimas alertas del detector (Fase 6) como DataFrame. Cacheado 30s.

    `storage.connect` garantiza que la tabla existe (aunque el detector no haya
    corrido nunca), asi que una BD sin alertas devuelve un DataFrame vacio.
    """
    conn = storage.connect(DB_PATH)
    try:
        df = pd.read_sql_query(
            "SELECT id, ts, market_question, market_slug, condition_id, wallet, username, "
            "side, trade_side, trade_size_usd, price_at_detection, score_total, "
            "score_breakdown, transaction_hash, "
            "COALESCE(scoring_version, 'v1') AS scoring_version FROM alerts "
            "WHERE score_total >= ? ORDER BY ts DESC LIMIT ?",
            conn, params=(min_score, limit),
        )
    finally:
        conn.close()
    return df


@st.cache_data(ttl=60)
def market_price_state(condition_id: str, outcome: str) -> dict[str, Any]:
    """Precio actual del outcome y si el mercado esta resuelto. Cacheado 60s.

    Devuelve {"status": "active"|"resolved"|"na", "price": float|None}. Usa Gamma
    por condition_id (da precio actual o final + flag closed en un solo sitio).
    """
    try:
        market = get_market_by_condition(condition_id)
    except Exception:  # noqa: BLE001 - fallo de API => N/A, no rompe la tabla
        return {"status": "na", "price": None}
    if not market:
        return {"status": "na", "price": None}
    outcomes = _parse_json_field(market.get("outcomes")) or []
    prices = [_to_float(p) for p in (_parse_json_field(market.get("outcomePrices")) or [])]
    if outcome not in outcomes:
        return {"status": "na", "price": None}
    idx = outcomes.index(outcome)
    price = prices[idx] if idx < len(prices) else None
    if price is None:
        return {"status": "na", "price": None}
    status = "resolved" if _is_truthy(market.get("closed")) else "active"
    return {"status": status, "price": price}


def detector_status() -> dict[str, Any]:
    """Ultimo heartbeat del detector desde service_health (ts, trades, vivo)."""
    conn = storage.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT ts, trades_processed, ws_connected FROM service_health "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"seen": False}
    ts, trades, ws = row
    age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 60.0
    return {"seen": True, "ts": ts, "trades_processed": trades, "ws_connected": bool(ws), "age_min": age_min}


def collector_status() -> dict[str, Any]:
    """Estado de la BD del collector: existe, nº de snapshots y antiguedad del ultimo."""
    if not DB_PATH.exists():
        return {"exists": False}
    conn = db.connect(DB_PATH)
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
    st.sidebar.header("Panel de control")
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
    st.subheader("Vista general")

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
    st.subheader("Detalle de mercado")

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
# Seccion 4 — ballenas
# --------------------------------------------------------------------------- #
def _short(addr: str) -> str:
    """Acorta una direccion (0xabcd...1234) para mostrarla en tablas."""
    return f"{addr[:6]}...{addr[-4:]}" if addr and len(addr) > 12 else addr


def render_whales(markets: list[dict[str, Any]]) -> None:
    """Top ballenas y, al seleccionar una, la foto actual de su cartera completa."""
    st.subheader("Ballenas")
    st.caption(
        "Foto ACTUAL (no historico). Las ballenas se rankean por VALOR EN $ "
        "(shares × precio actual) de su posicion agregada en los "
        f"{WHALE_MARKETS_SAMPLE} mercados de politica de mayor volumen."
    )

    politics_conditions = {m["condition_id"] for m in markets if m.get("condition_id")}
    if not politics_conditions:
        st.info("No hay mercados con condition_id para rankear ballenas.")
        return

    with st.spinner("Rankeando ballenas..."):
        whales = load_whales()
    if not whales:
        st.info("No se han encontrado holders en estos mercados.")
        return

    # Excluimos las wallets cuya cartera no tiene NADA de politica (operadores /
    # market makers cuyo valor son posiciones resueltas de otras categorias): no
    # nos interesan. De paso guardamos el % de su cartera que es politica.
    rows: list[dict[str, Any]] = []
    with st.spinner("Analizando carteras (excluyendo wallets sin política)..."):
        for w in whales:
            df = load_user_positions(w.proxy_wallet)
            pol, _tot = (
                politics_portfolio_share(df.to_dict("records"), politics_conditions)
                if not df.empty else (0.0, 0.0)
            )
            if pol <= 0:
                continue  # 0% de politica -> fuera
            rows.append({
                "Wallet": _short(w.proxy_wallet),
                "Valor en política ($)": w.total_value_usd,
                "% cartera en política": pol / _tot if _tot else 0.0,
                "Nº mercados": len(w.markets),
                "_wallet": w.proxy_wallet,
            })

    if not rows:
        st.info("Ninguna ballena con posiciones de política en su cartera ahora mismo.")
        return

    whale_df = pd.DataFrame(rows).sort_values("Valor en política ($)", ascending=False)
    st.caption(f"{len(rows)} de {len(whales)} ballenas tienen posiciones de política (el resto excluidas).")

    st.dataframe(
        whale_df.drop(columns="_wallet"),
        width="stretch",
        hide_index=True,
        column_config={
            "Valor en política ($)": st.column_config.NumberColumn(format="$%.0f"),
            "% cartera en política": st.column_config.ProgressColumn(
                "% cartera en política", min_value=0, max_value=1, format="%.0f%%"
            ),
        },
    )

    # --- Detalle de la cartera de una ballena --------------------------------
    st.markdown("#### Cartera de una ballena")
    options = {
        f"{_short(r['_wallet'])}  (${r['Valor en política ($)']:,.0f})": r["_wallet"]
        for r in whale_df.to_dict("records")
    }
    label = st.selectbox("Wallet", list(options.keys()))
    wallet = options[label]
    st.caption(f"Wallet completa: `{wallet}` · top {MAX_WHALE_POSITIONS} posiciones por valor")

    # Persistimos la cartera cargada en session_state: asi un rerun (p.ej. el
    # auto-refresco al entrar datos nuevos) la re-dibuja al instante en vez de
    # volver a cargarla y cancelar la carga a medias. Solo se carga cuando
    # cambia la wallet seleccionada.
    if st.session_state.get("whale_wallet") != wallet or "whale_positions" not in st.session_state:
        with st.spinner("Cargando cartera..."):
            st.session_state["whale_wallet"] = wallet
            st.session_state["whale_positions"] = load_user_positions(wallet)
    positions = st.session_state["whale_positions"]
    if positions.empty:
        st.warning("Esta wallet no tiene posiciones abiertas ahora mismo.")
        return

    # % de cartera = valor actual de la posicion / valor total de la cartera.
    positions["currentValue"] = pd.to_numeric(positions.get("currentValue"), errors="coerce").fillna(0.0)
    total_value = positions["currentValue"].sum()
    positions["pct_cartera"] = positions["currentValue"] / total_value if total_value else 0.0
    if "conditionId" in positions.columns:
        positions["es_politica"] = positions["conditionId"].isin(politics_conditions)
    else:
        positions["es_politica"] = False

    politics_value = positions.loc[positions["es_politica"], "currentValue"].sum()
    pct_politics = politics_value / total_value if total_value else 0.0

    c1, c2, c3 = st.columns(3)
    c1.metric("Valor (top posiciones)", f"${total_value:,.0f}",
              help=f"Suma de las {len(positions)} mayores posiciones por valor")
    c2.metric("Posiciones cargadas", len(positions))
    c3.metric("% en politica", f"{pct_politics:.0%}", help="Nuestro foco de analisis")

    table = pd.DataFrame({
        "Mercado": positions.get("title"),
        "Política": positions["es_politica"].map({True: "Sí", False: "—"}),
        "Apuesta": positions.get("outcome"),  # de qué lado está: Yes/No (Sí/No)
        "Tamaño": pd.to_numeric(positions.get("size"), errors="coerce"),
        "Precio medio": pd.to_numeric(positions.get("avgPrice"), errors="coerce"),
        "Valor actual": positions["currentValue"],
        "PnL": pd.to_numeric(positions.get("cashPnl"), errors="coerce"),
        "% cartera": positions["pct_cartera"],
    }).sort_values("Valor actual", ascending=False)

    st.dataframe(
        table,
        width="stretch",
        hide_index=True,
        column_config={
            "Valor actual": st.column_config.NumberColumn(format="$%.2f"),
            "PnL": st.column_config.NumberColumn(format="$%.2f"),
            "% cartera": st.column_config.ProgressColumn("% cartera", min_value=0, max_value=1, format="%.0f%%"),
        },
    )

    # Distribucion de la cartera entre mercados (top 15 por valor).
    st.markdown("#### Distribución de la cartera")
    chart_data = (
        table[["Mercado", "Valor actual"]]
        .dropna(subset=["Mercado"])
        .head(15)
        .set_index("Mercado")
    )
    st.bar_chart(chart_data)


# --------------------------------------------------------------------------- #
# Seccion 5 — alertas del detector (Fase 6)
# --------------------------------------------------------------------------- #
def _render_detector_status() -> None:
    """Estado del detector: ultimo heartbeat, antiguedad y aviso si parece caido."""
    status = detector_status()
    if not status["seen"]:
        st.info("El detector no ha escrito ningun heartbeat todavia. "
                "Arrancalo con: python -m src.realtime.stream")
        return

    age = status["age_min"]
    c1, c2, c3 = st.columns(3)
    estado = "activo" if age <= DETECTOR_DOWN_MINUTES and status["ws_connected"] else "sin señal"
    c1.metric("Detector", estado)
    c2.metric("Último heartbeat", f"hace {age:.0f} min")
    c3.metric("Trades procesados", f"{status['trades_processed']:,}")
    if age > DETECTOR_DOWN_MINUTES:
        st.warning(
            f"El último heartbeat es de hace {age:.0f} min (> {DETECTOR_DOWN_MINUTES:.0f}). "
            "El detector parece caído: revisa el proceso o logs/detector.log."
        )


def _render_breakdown(row: dict[str, Any]) -> None:
    """Muestra el score_breakdown (JSON) de una alerta, componente por componente."""
    st.markdown(f"**Desglose del score — total {int(row.get('score_total') or 0)}**")
    try:
        breakdown = json.loads(row.get("score_breakdown") or "{}")
    except (json.JSONDecodeError, TypeError):
        st.caption("No se pudo leer el desglose.")
        return

    silenciado = bool(breakdown.pop("flujo_toxico_silenciado", False))
    comp = pd.DataFrame(
        [{"Componente": k, "Puntos": v} for k, v in breakdown.items()]
    ).sort_values("Puntos", ascending=False)
    st.dataframe(comp, width="stretch", hide_index=True)
    if silenciado:
        st.caption("Nota: flujo tóxico silenciado (imbalance fuerte pero volumen de cubo insuficiente).")

    links = []
    if row.get("market_slug"):
        links.append(f"[Ver mercado en Polymarket]({POLYMARKET_EVENT_URL}/{row['market_slug']})")
    if row.get("transaction_hash"):
        links.append(f"[Transacción en Polygonscan]({POLYGONSCAN_TX_URL}/{row['transaction_hash']})")
    if links:
        st.markdown(" · ".join(links))


def render_alerts() -> None:
    """Pestaña de alertas del detector: estado, filtros y tabla con desglose."""
    st.subheader("Alertas")
    st.caption(
        "Anomalías compatibles con trading informado (no una acusación). "
        "El score y su desglose permiten calibrar con el backtest."
    )

    _render_detector_status()
    st.divider()

    c1, c2, c3 = st.columns([1, 2, 1])
    min_score = c1.slider("Score mínimo", 0, 100, 0, step=5)
    query = c2.text_input("Buscar mercado (título)", "")
    solo_v3 = c3.checkbox("Solo v3", value=True,
                          help="Ocultar las alertas v1/v2 (scorings antiguos, no comparables).")

    alerts = load_alerts(min_score, ALERTS_LIMIT)
    if not alerts.empty and query:
        alerts = alerts[alerts["market_question"].fillna("").str.contains(query, case=False)]

    n_legacy = int((alerts["scoring_version"] != "v3").sum()) if not alerts.empty else 0
    if solo_v3 and not alerts.empty:
        alerts = alerts[alerts["scoring_version"] == "v3"]
    if n_legacy:
        st.warning(
            f"{n_legacy} alertas son **v1/v2**: scorings antiguos y no comparables "
            "(v1 con el perfilado inactivo; v2 con track_record activo sobre un "
            "win_rate falso, ver Fase 1c). Su score no es fiable; no las "
            "interpretes ni las mezcles con las v3."
            + ("" if solo_v3 else " Están visibles porque desmarcaste «Solo v3».")
        )

    if alerts.empty:
        st.info("No hay alertas que mostrar todavía (con estos filtros).")
        return

    def _lado(r: pd.Series) -> str:
        outcome = r.get("side") or ""
        ts = r.get("trade_side") or ""
        return f"{outcome} ({ts})" if ts else str(outcome)

    def _pnl(r: pd.Series) -> str:
        # P&L al precio actual del outcome (o payout final si esta resuelto).
        state = market_price_state(r.get("condition_id") or "", r.get("side") or "")
        return format_pnl_cell(r.get("price_at_detection"), r.get("trade_size_usd"),
                               r.get("trade_side"), state)

    view = pd.DataFrame({
        "Fecha": pd.to_datetime(alerts["ts"], errors="coerce"),
        "Mercado": alerts["market_question"],
        "Wallet": alerts["wallet"].map(_short),
        "Usuario": alerts["username"],
        "Lado": alerts.apply(_lado, axis=1),
        "Tamaño ($)": pd.to_numeric(alerts["trade_size_usd"], errors="coerce"),
        "Score": pd.to_numeric(alerts["score_total"], errors="coerce"),
        "Versión": alerts["scoring_version"],
        "P&L": alerts.apply(_pnl, axis=1),
    })

    def _pnl_color(val: Any) -> str:
        """Verde si ganancia, rojo si perdida, gris en el resto (0, N/A, Resuelto)."""
        if isinstance(val, str) and val.startswith("+$"):
            return "color: #16a34a"   # verde
        if isinstance(val, str) and val.startswith("-$"):
            return "color: #dc2626"   # rojo
        return "color: #6b7280"       # gris

    styled = view.style.map(_pnl_color, subset=["P&L"])

    event = st.dataframe(
        styled,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Fecha": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm"),
            "Tamaño ($)": st.column_config.NumberColumn(format="$%.0f"),
            "Score": st.column_config.NumberColumn(format="%d"),
        },
    )

    selected = event.selection.rows if hasattr(event, "selection") else []
    if selected:
        row = alerts.iloc[selected[0]].to_dict()
        st.divider()
        _render_breakdown(row)
    else:
        st.caption("Selecciona una fila para ver el desglose del score.")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    """Punto de entrada del dashboard."""
    st.markdown(
        '<div class="app-header">'
        '<h1>Polymarket Politics</h1>'
        '<div class="subtitle">Monitor de mercados de política y de las mayores posiciones (ballenas)</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    cfg = render_sidebar()

    try:
        markets = load_markets()
    except Exception as exc:  # noqa: BLE001 - mostrar el fallo en la UI, no crashear
        st.error(f"Error cargando mercados: {exc}")
        return

    tab_overview, tab_detail, tab_whales, tab_alerts = st.tabs(
        ["Mercados", "Detalle", "Ballenas", "Alertas"]
    )
    with tab_overview:
        render_overview(markets)
    with tab_detail:
        render_detail(markets)
    with tab_whales:
        render_whales(markets)
    with tab_alerts:
        render_alerts()

    if cfg["auto"]:
        time.sleep(cfg["every"])
        st.rerun()


if __name__ == "__main__":
    main()
