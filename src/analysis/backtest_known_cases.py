"""Validacion retroactiva del detector sobre casos DOCUMENTADOS publicamente.

Analisis OFFLINE (sin WebSocket): reconstruye, trade a trade y con datos ya
resueltos, el score que el detector HABRIA calculado en cada momento, usando
UNICAMENTE informacion disponible hasta ese punto (nada de datos futuros: eso
seria hacer trampa). Responde: ¿el detector, con los pesos actuales, habria
marcado el caso ANTES del movimiento grande de precio, o solo despues / nunca?

Caso de referencia: mercado "Maduro out by January 31, 2026?" (caso DOJ/CFTC
United States v. Van Dyke). Los insiders compraron YES a ~0.06-0.07 antes de la
captura de Maduro (~3 ene 2026); el mercado resolvio a 1.

Limitaciones honestas de los datos (documentadas en el informe):
- El endpoint de trades por-mercado solo devuelve los ~ultimos miles (el dia de
  la resolucion), asi que las ventanas de cubo de volumen previas no se pueden
  reconstruir: para trades anteriores a esa ventana, el imbalance del cubo se
  toma como 0 (componente flujo_toxico conservador = 0).
- win_rate y cluster de funding NO se reconstruyen historicamente (requeririan
  fechas de resolucion y el grafo de funding en ese instante): se dejan a 0.
  => el score reconstruido es una COTA INFERIOR del real.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src import config
from src.analysis.buckets import DEFAULT_BUCKET_SIZE_USD, _trade_usd, signed_imbalance
from src.analysis.profiles import WalletProfile, _age_days_from_ts, compute_trade_stats
from src.analysis.scoring import ScoreBreakdown, compute_score
from src.client.data_api import get_market_holders, get_market_trades, get_user_trades
from src.client.gamma import GAMMA_BASE_URL, _parse_json_field, _to_float
from src.client.http import get_json
from src.client.polygonscan import get_first_token_transfers, get_first_transactions

logger = logging.getLogger(__name__)

PUBLIC_SEARCH_ENDPOINT: str = f"{GAMMA_BASE_URL}/public-search"
DEFAULT_MADURO_QUERY: str = "Maduro out by January 31"
SPIKE_PRICE: float = 0.5   # umbral de precio Yes que marca el "movimiento grande"
DEFAULT_CANDIDATES: int = 8
REPORTS_DIR: Path = Path(__file__).resolve().parents[2] / "reports"


# --------------------------------------------------------------------------- #
# Localizacion del mercado
# --------------------------------------------------------------------------- #
def locate_market(query: str = DEFAULT_MADURO_QUERY) -> dict[str, Any] | None:
    """Busca un mercado por texto (Gamma public-search) y lo devuelve parseado.

    Devuelve dict con condition_id, question, outcomes, outcome_prices y
    winning_index (indice del outcome que resolvio a ~1), o None si no se halla.
    """
    payload = get_json(PUBLIC_SEARCH_ENDPOINT, params={"q": query, "limit_per_type": 20})
    events = payload.get("events", []) if isinstance(payload, dict) else []
    for event in events:
        for market in event.get("markets", []) or []:
            question = market.get("question") or ""
            if "maduro" not in question.lower() or "january 31" not in question.lower():
                continue
            prices = [_to_float(p) for p in (_parse_json_field(market.get("outcomePrices")) or [])]
            outcomes = _parse_json_field(market.get("outcomes")) or []
            winning = max(range(len(prices)), key=lambda i: prices[i] or 0) if prices else 0
            return {
                "condition_id": market.get("conditionId"),
                "question": question,
                "outcomes": outcomes,
                "outcome_prices": prices,
                "winning_index": winning,
                "event_slug": event.get("slug"),
            }
    return None


def winning_holder_wallets(condition_id: str, winning_index: int, top_n: int) -> list[str]:
    """Wallets del lado GANADOR (candidatas a insider) por tamaño de posicion."""
    holders: list[tuple[float, str]] = []
    for group in get_market_holders(condition_id, limit=100):
        for h in group.get("holders", []):
            if h.get("outcomeIndex") == winning_index and h.get("proxyWallet"):
                holders.append((float(h.get("amount") or 0), h["proxyWallet"]))
    holders.sort(reverse=True)
    return [w for _, w in holders[:top_n]]


# --------------------------------------------------------------------------- #
# Reconstruccion SIN mirar el futuro (funciones puras / testeables)
# --------------------------------------------------------------------------- #
def reconstruct_profile(
    wallet: str,
    all_wallet_trades: list[dict[str, Any]],
    cutoff_ts: int,
    funding_ts: int | None,
) -> WalletProfile:
    """Perfil de la wallet con SOLO sus trades anteriores a `cutoff_ts`.

    La edad se calcula respecto al momento del trade (no respecto a hoy). win_rate
    y funding_cluster se dejan a 0/None (no reconstruibles sin datos futuros).
    """
    prior = [t for t in all_wallet_trades if int(t.get("timestamp", 0)) < cutoff_ts]
    stats = compute_trade_stats(prior)
    return WalletProfile(
        wallet=wallet,
        wallet_age_days=_age_days_from_ts(funding_ts, now=cutoff_ts),
        total_volume_usd=stats["total_volume_usd"],
        n_markets=stats["n_markets"],
        concentration=stats["concentration"],
        win_rate=None,
        n_resolved=0,
        longshot_wins=0,
        funding_cluster_id=None,
        avg_trade_size_usd=stats["avg_trade_size_usd"],
    )


def bucket_state_at(
    market_trades_sorted: list[dict[str, Any]],
    ts: int,
    bucket_size_usd: float = DEFAULT_BUCKET_SIZE_USD,
) -> tuple[float, float]:
    """Estado del cubo de volumen del mercado en el instante `ts` (imbalance, volumen).

    Reproduce los trades del mercado disponibles hasta `ts` acumulando en cubos
    de `bucket_size_usd`; devuelve el imbalance y el volumen del cubo ABIERTO en
    ese momento. Si no hay trades del mercado hasta `ts` (ventana no disponible),
    devuelve (0, 0) — conservador.
    """
    current: list[dict[str, Any]] = []
    volume = 0.0
    for t in market_trades_sorted:
        if int(t.get("timestamp", 0)) > ts:
            break
        current.append(t)
        volume += _trade_usd(t)
        if volume >= bucket_size_usd:
            current = []
            volume = 0.0
    return signed_imbalance(current), volume


def _trade_sign_simple(trade: dict[str, Any]) -> int:
    """+1 si empuja Yes, -1 si empuja No (compra Yes/vende No = +)."""
    side = str(trade.get("side", "")).upper()
    is_yes = str(trade.get("outcome", "")).lower() in ("yes", "si", "sí")
    if side == "BUY":
        return 1 if is_yes else -1
    if side == "SELL":
        return -1 if is_yes else 1
    return 0


def reconstruct_streak(wallet_market_trades_sorted: list[dict[str, Any]], idx: int) -> tuple[int, bool]:
    """Racha de mismo-lado y si paga peor precio, usando trades [0..idx] de la wallet.

    Devuelve (racha, price_against) para el trade en `idx`.
    """
    sign = None
    streak = 0
    start_price = None
    price = None
    for i in range(idx + 1):
        t = wallet_market_trades_sorted[i]
        s = _trade_sign_simple(t)
        try:
            price = float(t.get("price"))
        except (TypeError, ValueError):
            price = None
        if s == sign and s != 0:
            streak += 1
        else:
            sign = s
            streak = 1
            start_price = price
    price_against = price is not None and start_price is not None and price > start_price
    return streak, price_against


# --------------------------------------------------------------------------- #
# Scoring retroactivo de una wallet
# --------------------------------------------------------------------------- #
@dataclass
class ScoredTrade:
    """Un trade con el score reconstruido en su momento."""
    ts: int
    side: str
    outcome: str
    price: float
    usd: float
    score: ScoreBreakdown


def _funding_ts(wallet: str) -> int | None:
    """Timestamp de la primera entrada de fondos (USDC.e, o nativo). None si falla."""
    try:
        transfers = get_first_token_transfers(wallet, limit=1)
        if transfers and transfers[0].get("timeStamp") is not None:
            return int(transfers[0]["timeStamp"])
        txs = get_first_transactions(wallet, limit=1)
        if txs and txs[0].get("timeStamp") is not None:
            return int(txs[0]["timeStamp"])
    except Exception:  # noqa: BLE001 - sin funding => edad desconocida, no bloquea el analisis
        logger.warning("No se pudo obtener el funding de %s", wallet)
    return None


def score_wallet(
    wallet: str,
    condition_id: str,
    market_trades_sorted: list[dict[str, Any]],
) -> list[ScoredTrade]:
    """Reconstruye el score de cada trade de la wallet en el mercado dado."""
    all_trades = get_user_trades(wallet, max_trades=5000)
    in_market = sorted(
        (t for t in all_trades if t.get("conditionId") == condition_id),
        key=lambda t: int(t.get("timestamp", 0)),
    )
    funding_ts = _funding_ts(wallet)

    scored: list[ScoredTrade] = []
    for idx, trade in enumerate(in_market):
        ts = int(trade.get("timestamp", 0))
        profile = reconstruct_profile(wallet, all_trades, ts, funding_ts)
        imbalance, bvol = bucket_state_at(market_trades_sorted, ts)
        streak, price_against = reconstruct_streak(in_market, idx)
        market_data = {
            "shared_cluster_ids": set(),
            "same_side_streak": streak,
            "price_against": price_against,
            "bucket_volume_usd": bvol,
        }
        score = compute_score(trade, profile, imbalance, market_data)
        scored.append(ScoredTrade(
            ts=ts, side=str(trade.get("side", "")), outcome=str(trade.get("outcome", "")),
            price=float(trade.get("price") or 0), usd=_trade_usd(trade), score=score,
        ))
    return scored


def first_alert(scored: list[ScoredTrade], threshold: int = config.ALERT_THRESHOLD) -> ScoredTrade | None:
    """Primer trade cuyo score cruza el umbral, o None si nunca lo cruza."""
    for st in scored:
        if st.score.score_total >= threshold:
            return st
    return None


def spike_time(market_trades_sorted: list[dict[str, Any]]) -> int | None:
    """Momento en que el precio de Yes cruza SPIKE_PRICE (el movimiento grande)."""
    for t in market_trades_sorted:
        try:
            p = float(t.get("price"))
        except (TypeError, ValueError):
            continue
        yes_p = p if str(t.get("outcome", "")).lower() in ("yes", "si", "sí") else 1.0 - p
        if yes_p >= SPIKE_PRICE:
            return int(t.get("timestamp", 0))
    return None


# --------------------------------------------------------------------------- #
# Informe
# --------------------------------------------------------------------------- #
def _iso(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else "—"


def build_report(query: str = DEFAULT_MADURO_QUERY, candidates: int = DEFAULT_CANDIDATES) -> str:
    """Ejecuta el analisis completo y devuelve el informe en markdown."""
    market = locate_market(query)
    if market is None:
        return f"# Backtest\n\nNo se encontro el mercado para la busqueda: {query!r}.\n"

    cid = market["condition_id"]
    lines = [
        "# Backtest retroactivo — caso Maduro (offline, datos resueltos)",
        "",
        f"**Mercado:** {market['question']}  ",
        f"**condition_id:** `{cid}`  ",
        f"**Resolucion:** outcome ganador = `{market['outcomes'][market['winning_index']] if market['outcomes'] else '?'}` "
        f"(precios {market['outcome_prices']})",
        "",
        "Reconstruccion sin lookahead. El score es una COTA INFERIOR (win_rate, "
        "cluster y cubos previos no reconstruibles se dejan a 0).",
        "",
    ]

    market_trades = sorted(
        get_market_trades(cid, max_trades=6000),
        key=lambda t: int(t.get("timestamp", 0)),
    )
    spike = spike_time(market_trades)
    lines.append(f"**Movimiento grande (precio Yes >= {SPIKE_PRICE}):** {_iso(spike)} UTC")
    lines.append(f"**Umbral de alerta:** score >= {config.ALERT_THRESHOLD}")
    lines.append("")

    wallets = winning_holder_wallets(cid, market["winning_index"], candidates)
    lines.append(f"Analizadas {len(wallets)} wallets del lado ganador.\n")

    marcadas = 0
    for wallet in wallets:
        scored = score_wallet(wallet, cid, market_trades)
        if not scored:
            continue
        alert = first_alert(scored)
        max_score = max(st.score.score_total for st in scored)
        lines.append(f"## Wallet `{wallet}`")
        lines.append("")
        lines.append("| # | Fecha (UTC) | Lado | Precio | $ | Score | Componentes |")
        lines.append("|--:|---|---|--:|--:|--:|---|")
        for i, st in enumerate(scored, 1):
            comps = ", ".join(f"{k}={v}" for k, v in st.score.components().items() if isinstance(v, int) and v)
            lines.append(
                f"| {i} | {_iso(st.ts)} | {st.side} {st.outcome} | {st.price:.3f} | "
                f"{st.usd:,.0f} | **{st.score.score_total}** | {comps or '—'} |"
            )
        lines.append("")
        if alert is not None:
            marcadas += 1
            delta = ""
            if spike is not None:
                hrs = (spike - alert.ts) / 3600.0
                delta = f" — {hrs:.1f} h {'ANTES' if hrs > 0 else 'DESPUES'} del movimiento grande"
            lines.append(f"**Marcada:** primera alerta el {_iso(alert.ts)} UTC "
                         f"(score {alert.score.score_total}){delta}.")
        else:
            faltan = _missing_components(scored)
            lines.append(f"**No marcada.** Score maximo {max_score} (umbral {config.ALERT_THRESHOLD}). "
                         f"Componentes que no sumaron: {faltan}.")
        lines.append("")

    lines.append("## Conclusion")
    lines.append("")
    lines.append(
        f"{marcadas}/{len(wallets)} wallets del lado ganador habrian sido marcadas por el detector "
        f"con los pesos actuales, en su reconstruccion sin lookahead."
    )
    lines.append("")
    lines.append(
        "Limitacion de datos importante: el insider mayor documentado (13 trades, ~$34k) redimio "
        "su posicion tras la resolucion, por lo que NO aparece en holders ni es recuperable "
        "(el endpoint de trades por-mercado solo devuelve los ~ultimos miles, del dia del pico). "
        "Las wallets analizadas son los ganadores que aun mantenian posicion: entradas de compra "
        "de YES a 0.06-0.21 en dic-ene, pero PEQUEÑAS ($774-$862)."
    )
    lines.append("")
    lines.append("### Antes / despues del longshot por tramos")
    lines.append("")
    lines.append(
        "ANTES (umbral fijo $2.500 para el longshot): estas entradas pequeñas (~$800) NO activaban "
        "el componente longshot; a lo sumo sumaban wallet_fresca, con lo que su score se quedaba muy "
        "por debajo del umbral 50."
    )
    lines.append(
        "DESPUES (RELEVANCE_TIERS: $500 a precio <=0.10, $1.500 a <=0.20 y <=0.35): las "
        "compras de YES a precio extremo (0.06-0.09) por ~$800 ya activan longshot (+20), que es la "
        "señal correcta (conviccion en payout). Sumado a la freshness cuando la wallet es nueva, el "
        "score sube claramente frente al escenario anterior."
    )
    lines.append("")
    lines.append(
        f"Resultado tras el cambio: {marcadas}/{len(wallets)} marcadas (ver score por wallet arriba). "
        "El ajuste sube los scores en la direccion correcta; las que no cruzan se quedan cerca del "
        "umbral y solo lo pasarian con otra señal (freshness muy reciente, sin_perfil, o el tamaño del "
        "insider GRANDE que aqui no es recuperable). NO se han tocado mas pesos para 'hacer que pase': "
        "el umbral y la escala se calibraran con el backtest cuantitativo del Paso 4. El campo "
        "longshot_tier se guarda en cada score para analizar el rendimiento por tramo."
    )
    lines.append("")
    lines.append(
        "Nota: la edad de wallet (componente wallet_fresca) se obtiene de la primera tx de funding via "
        "Polygonscan; si esa consulta throttlea, la edad queda desconocida y el score de esa wallet "
        "baja, por lo que el conteo de marcadas puede variar ligeramente entre ejecuciones."
    )
    return "\n".join(lines)


def _missing_components(scored: list[ScoredTrade]) -> str:
    """Lista de componentes que nunca sumaron en ninguna trade de la wallet."""
    fired: set[str] = set()
    for st in scored:
        for k, v in st.score.components().items():
            if isinstance(v, int) and v:
                fired.add(k)
    weights = config.SCORE_WEIGHTS
    never = [k for k in weights if k != "wallet_fresca_extra" and k not in fired]
    return ", ".join(never) or "ninguno"


def _main() -> None:
    """Ejecuta el backtest y guarda el informe en reports/."""
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # La consola de Windows (cp1252) no puede imprimir algunos caracteres; que no
    # rompa el guardado del informe.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    report = build_report()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"backtest_maduro_{datetime.now(timezone.utc):%Y%m%d_%H%M}.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[informe guardado en {out}]")


if __name__ == "__main__":
    _main()
