"""Capa lenta de la Fase 6: perfiles de wallet (batch, cada 4-6 horas).

Para cada wallet vista en mercados de politica calcula rasgos que la capa
rapida (scoring en tiempo real) consultara sin llamadas HTTP: edad, volumen,
concentracion, win-rate en mercados ya resueltos, longshots ganados y cluster
de funding. Se persisten en la tabla `wallet_profiles` del SQLite del proyecto.

Nota honesta: el win-rate se aproxima a partir de las posiciones abiertas cuyo
mercado ya ha resuelto (curPrice ~0/1 o redeemable). Las posiciones ya
redimidas pueden no aparecer, asi que es un indicador, no una cifra exacta.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from src import db
from src.client.data_api import NEUTRAL_SORT_BY, get_user_positions, get_user_trades
from src.client.polygonscan import get_first_token_transfers, get_first_transactions
from src.collector.models import DB_PATH

logger = logging.getLogger(__name__)

# Umbrales para clasificar posiciones (precio actual del outcome que se tiene).
RESOLVED_WIN_PRICE: float = 0.98   # >= => el outcome que tiene practicamente gano
RESOLVED_LOSS_PRICE: float = 0.02  # <= => practicamente perdio
LONGSHOT_PROB: float = 0.35        # entrada por debajo de esta prob = longshot

# Tope de posiciones a traer para el win_rate. Alto a proposito: solo perfilamos
# wallets que pasan el filtro de $2.500 y como mucho una vez por TTL, asi que el
# coste es asumible y evita truncar wallets muy activas (544 mercados => >500).
PROFILE_MAX_POSITIONS: int = 2000

WALLET_PROFILES_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS wallet_profiles (
    wallet              TEXT PRIMARY KEY,
    wallet_age_days     REAL,
    total_volume_usd    REAL,
    n_markets           INTEGER,
    concentration       REAL,   -- HHI sobre volumen por mercado (1/n..1)
    win_rate            REAL,   -- NULL si no hay posiciones resueltas
    n_resolved          INTEGER,
    longshot_wins       INTEGER,
    funding_cluster_id  TEXT,   -- direccion del funder (wallets hermanas la comparten)
    avg_trade_size_usd  REAL,
    win_rate_reliable   INTEGER,-- 0 si la muestra se trunco (win_rate sesgado, no usar)
    updated_at          TEXT
);
"""

# Columnas añadidas despues del esquema original; se migran con ALTER para BDs
# que ya existian sin ellas (idempotente).
_PROFILE_EXTRA_COLUMNS: dict[str, str] = {
    "win_rate_reliable": "INTEGER",
}


@dataclass
class WalletProfile:
    """Rasgos precomputados de una wallet para el scoring en tiempo real."""

    wallet: str
    wallet_age_days: float | None = None
    total_volume_usd: float = 0.0
    n_markets: int = 0
    concentration: float = 0.0
    win_rate: float | None = None
    n_resolved: int = 0
    longshot_wins: int = 0
    funding_cluster_id: str | None = None
    avg_trade_size_usd: float = 0.0
    win_rate_reliable: bool = True
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# --------------------------------------------------------------------------- #
# Funciones puras (sin red) — testeables
# --------------------------------------------------------------------------- #
def _trade_usd(trade: dict[str, Any]) -> float:
    """Valor en $ de un trade = size (shares) × price."""
    try:
        return float(trade.get("size") or 0.0) * float(trade.get("price") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def compute_trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Estadisticas de actividad a partir del historial de trades de una wallet.

    Devuelve total_volume_usd, n_markets, concentration (HHI del volumen por
    mercado), avg_trade_size_usd y first_trade_ts (unix, el mas antiguo visto).
    """
    volume_by_market: dict[str, float] = {}
    total = 0.0
    n = 0
    first_ts: int | None = None
    for t in trades:
        usd = _trade_usd(t)
        total += usd
        n += 1
        cid = t.get("conditionId")
        if cid:
            volume_by_market[cid] = volume_by_market.get(cid, 0.0) + usd
        ts = t.get("timestamp")
        if ts is not None:
            ts = int(ts)
            first_ts = ts if first_ts is None else min(first_ts, ts)

    concentration = 0.0
    if total > 0:
        concentration = sum((v / total) ** 2 for v in volume_by_market.values())

    return {
        "total_volume_usd": total,
        "n_markets": len(volume_by_market),
        "concentration": concentration,
        "avg_trade_size_usd": total / n if n else 0.0,
        "first_trade_ts": first_ts,
    }


def compute_win_stats(positions: list[dict[str, Any]]) -> dict[str, Any]:
    """Win-rate aproximado a partir de posiciones cuyo mercado ya ha resuelto.

    Resuelta = curPrice practicamente 0 o 1 (o redeemable). Ganada = curPrice
    ~1 o redeemable. Longshot ganado = ganada con precio de entrada (avgPrice)
    por debajo de LONGSHOT_PROB. Devuelve win_rate (None si no hay resueltas),
    n_resolved y longshot_wins.

    OJO CON EL SESGO DE SUPERVIVENCIA: este calculo asume que `positions` es una
    muestra NO sesgada respecto al resultado. Si las posiciones se pidieron
    ordenadas por valor actual (CURRENT/DESC) y se truncaron, las perdedoras
    (valor ~$0) quedan fuera y el win_rate sale inflado (visto en produccion:
    win_rate 0.99). Pide las posiciones con `sort_by=NEUTRAL_SORT_BY` y, si la
    lista viene truncada, marca el win_rate como no fiable (ver build_profile).
    """
    n_resolved = 0
    wins = 0
    longshot_wins = 0
    for p in positions:
        try:
            cur = float(p.get("curPrice"))
        except (TypeError, ValueError):
            continue
        redeemable = bool(p.get("redeemable"))
        won = redeemable or cur >= RESOLVED_WIN_PRICE
        lost = (not won) and cur <= RESOLVED_LOSS_PRICE
        if not (won or lost):
            continue  # sigue abierta
        n_resolved += 1
        if won:
            wins += 1
            try:
                avg = float(p.get("avgPrice") or 1.0)
            except (TypeError, ValueError):
                avg = 1.0
            if avg < LONGSHOT_PROB:
                longshot_wins += 1

    win_rate = (wins / n_resolved) if n_resolved else None
    return {"win_rate": win_rate, "n_resolved": n_resolved, "longshot_wins": longshot_wins}


def _age_days_from_ts(unix_ts: int | None, now: float | None = None) -> float | None:
    """Convierte un timestamp unix en dias de antiguedad hasta ahora."""
    if unix_ts is None:
        return None
    now = time.time() if now is None else now
    return max(0.0, (now - unix_ts) / 86400.0)


# --------------------------------------------------------------------------- #
# Orquestacion (con red)
# --------------------------------------------------------------------------- #
def _funding(wallet: str) -> tuple[str | None, int | None]:
    """(funder, primer_ts_funding) via Polygonscan: USDC.e primero, nativo fallback."""
    transfers = get_first_token_transfers(wallet, limit=1)
    if transfers:
        ts = transfers[0].get("timeStamp")
        return transfers[0].get("from"), int(ts) if ts is not None else None
    txs = get_first_transactions(wallet, limit=1)
    if txs:
        ts = txs[0].get("timeStamp")
        return txs[0].get("from"), int(ts) if ts is not None else None
    return None, None


def build_profile(wallet: str, max_trades: int | None = 2000) -> WalletProfile:
    """Construye el perfil completo de una wallet (llama a la Data API y Polygonscan)."""
    trades = get_user_trades(wallet, max_trades=max_trades)
    # Orden NEUTRAL respecto al resultado (no CURRENT/DESC): evita el sesgo de
    # supervivencia en el win_rate. Tope alto para no truncar wallets activas.
    positions = get_user_positions(
        wallet, max_positions=PROFILE_MAX_POSITIONS, sort_by=NEUTRAL_SORT_BY
    )
    # Si volvieron TANTAS como el tope, la muestra esta truncada => el win_rate
    # puede estar sesgado y no debe usarse para puntuar.
    win_rate_reliable = len(positions) < PROFILE_MAX_POSITIONS
    funder, funding_ts = _funding(wallet)

    ts_stats = compute_trade_stats(trades)
    win_stats = compute_win_stats(positions)

    # Edad = por la tx de funding; si no hay, por el trade mas antiguo visto.
    age = _age_days_from_ts(funding_ts)
    if age is None:
        age = _age_days_from_ts(ts_stats["first_trade_ts"])

    return WalletProfile(
        wallet=wallet,
        wallet_age_days=age,
        total_volume_usd=ts_stats["total_volume_usd"],
        n_markets=ts_stats["n_markets"],
        concentration=ts_stats["concentration"],
        win_rate=win_stats["win_rate"],
        n_resolved=win_stats["n_resolved"],
        longshot_wins=win_stats["longshot_wins"],
        funding_cluster_id=funder,
        avg_trade_size_usd=ts_stats["avg_trade_size_usd"],
        win_rate_reliable=win_rate_reliable,
    )


def ensure_profiles_schema(conn: sqlite3.Connection) -> None:
    """Crea la tabla wallet_profiles y migra columnas nuevas (idempotente).

    Las filas que ya existian (perfiladas antes del arreglo del sesgo de
    supervivencia) se marcan con win_rate_reliable = 0: su win_rate se calculo
    con la muestra truncada y no es fiable hasta que se reconstruya el perfil.
    """
    conn.executescript(WALLET_PROFILES_SCHEMA)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(wallet_profiles)")}
    added_reliable = "win_rate_reliable" not in existing
    for col, decl in _PROFILE_EXTRA_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE wallet_profiles ADD COLUMN {col} {decl}")
    if added_reliable:
        conn.execute("UPDATE wallet_profiles SET win_rate_reliable = 0 WHERE win_rate_reliable IS NULL")
    conn.commit()


def connect(db_path: Any = DB_PATH) -> sqlite3.Connection:
    """Abre el SQLite del proyecto y garantiza la tabla wallet_profiles."""
    conn = db.connect(db_path)
    ensure_profiles_schema(conn)
    return conn


_PROFILE_COLUMNS = (
    "wallet, wallet_age_days, total_volume_usd, n_markets, concentration, win_rate, "
    "n_resolved, longshot_wins, funding_cluster_id, avg_trade_size_usd, win_rate_reliable, updated_at"
)


def load_profile(conn: sqlite3.Connection, wallet: str) -> WalletProfile | None:
    """Lee el perfil de una wallet de SQLite (rapido, sin red). None si no existe."""
    row = conn.execute(
        f"SELECT {_PROFILE_COLUMNS} FROM wallet_profiles WHERE wallet = ?", (wallet,)
    ).fetchone()
    if row is None:
        return None
    return WalletProfile(*row)


def load_shared_cluster_ids(conn: sqlite3.Connection) -> set[str]:
    """Funders (funding_cluster_id) compartidos por mas de una wallet perfilada."""
    rows = conn.execute(
        "SELECT funding_cluster_id FROM wallet_profiles "
        "WHERE funding_cluster_id IS NOT NULL "
        "GROUP BY funding_cluster_id HAVING COUNT(*) > 1"
    ).fetchall()
    return {r[0] for r in rows}


def save_profile(conn: sqlite3.Connection, profile: WalletProfile) -> None:
    """Inserta o actualiza el perfil de una wallet."""
    d = asdict(profile)
    cols = ", ".join(d.keys())
    placeholders = ", ".join(f":{k}" for k in d)
    conn.execute(f"INSERT OR REPLACE INTO wallet_profiles ({cols}) VALUES ({placeholders})", d)
    conn.commit()


def build_profiles_for(wallets: list[str], conn: sqlite3.Connection | None = None) -> int:
    """Construye y guarda perfiles para una lista de wallets. Devuelve cuantos."""
    own = conn is None
    conn = conn or connect()
    n = 0
    try:
        for wallet in wallets:
            try:
                profile = build_profile(wallet)
                save_profile(conn, profile)
                n += 1
                logger.info(
                    "Perfil %s: vol=$%.0f mercados=%d win_rate=%s edad=%.0fd",
                    wallet, profile.total_volume_usd, profile.n_markets,
                    f"{profile.win_rate:.0%}" if profile.win_rate is not None else "n/a",
                    profile.wallet_age_days or 0.0,
                )
            except Exception:  # noqa: BLE001 - un perfil fallido no debe parar el batch
                logger.exception("Fallo construyendo perfil de %s", wallet)
    finally:
        if own:
            conn.close()
    return n


def _main() -> None:
    """Demo: perfila el top de ballenas por valor en politica y las guarda."""
    from src.analysis.whales import rank_whales
    from src.client.gamma import flatten_markets, get_politics_events

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    markets = flatten_markets(get_politics_events())
    whales = rank_whales(markets, top_n=10)
    n = build_profiles_for([w.proxy_wallet for w in whales])
    print(f"\n{n} perfiles guardados en wallet_profiles.")


if __name__ == "__main__":
    _main()
