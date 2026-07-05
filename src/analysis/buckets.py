"""Paso 2 de la Fase 6: cubos de volumen (reloj de volumen, estilo VPIN).

En vez de medir el flujo "cada X minutos" (reloj de pared), se acumulan los
trades de un mercado en cubos de igual volumen en $ y, al cerrarse cada cubo,
se mide el desequilibrio compra/venta. Cuando el mercado esta muerto no se
computa nada; cuando entra dinero fuerte, el muestreo se acelera solo.

Todo el signo se normaliza al "espacio Yes": comprar Yes = +1, vender Yes = -1;
y como comprar No equivale a apostar contra Yes, comprar No = -1 y vender No = +1.
Asi el imbalance de un cubo es +1 si todo el flujo empuja Yes y -1 si empuja No.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src import db
from src.collector.models import DB_PATH

logger = logging.getLogger(__name__)

DEFAULT_BUCKET_SIZE_USD: float = 5000.0

VOLUME_BUCKETS_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS volume_buckets (
    condition_id      TEXT,
    bucket_seq        INTEGER,
    ts_open           TEXT,
    ts_close          TEXT,
    volume_usd        REAL,
    signed_imbalance  REAL,   -- (-1..+1) en espacio Yes
    price_open        REAL,
    price_close       REAL,
    PRIMARY KEY (condition_id, bucket_seq)
);
"""


def _trade_usd(trade: dict[str, Any]) -> float:
    """Valor en $ de un trade = size (shares) × price."""
    try:
        return float(trade.get("size") or 0.0) * float(trade.get("price") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_yes(trade: dict[str, Any]) -> bool:
    """True si el outcome del trade es el lado 'Yes' (Si)."""
    return str(trade.get("outcome", "")).strip().lower() in ("yes", "si", "sí")


def _trade_sign(trade: dict[str, Any]) -> int:
    """Signo del trade en espacio Yes: +1 empuja Yes, -1 empuja No, 0 desconocido.

    signo = direccion_compra × direccion_yes, donde comprar=+1/vender=-1 y
    Yes=+1/No=-1. Asi: comprar Yes=+1, vender Yes=-1, comprar No=-1, vender No=+1.
    """
    side = str(trade.get("side", "")).strip().upper()
    if side == "BUY":
        buy_dir = 1
    elif side == "SELL":
        buy_dir = -1
    else:
        return 0
    return buy_dir * (1 if _is_yes(trade) else -1)


def _yes_price(trade: dict[str, Any]) -> float | None:
    """Precio del trade normalizado a probabilidad de Yes (1 - p si el trade es No)."""
    try:
        p = float(trade.get("price"))
    except (TypeError, ValueError):
        return None
    return p if _is_yes(trade) else 1.0 - p


def signed_imbalance(trades_bucket: list[dict[str, Any]]) -> float:
    """Desequilibrio compra/venta de un cubo, ponderado por volumen, en (-1..+1).

    Suma el signo de cada trade (espacio Yes) ponderado por su volumen en $ y lo
    normaliza por el volumen total del cubo. +1 = todo el dinero empuja Yes,
    -1 = todo empuja No, ~0 = equilibrado. Cubo vacio -> 0.0.
    """
    num = 0.0
    vol = 0.0
    for t in trades_bucket:
        usd = _trade_usd(t)
        vol += usd
        num += _trade_sign(t) * usd
    return (num / vol) if vol else 0.0


def bucket_trades(
    condition_id: str,
    trades_list: list[dict[str, Any]],
    bucket_size_usd: float = DEFAULT_BUCKET_SIZE_USD,
) -> list[dict[str, Any]]:
    """Divide los trades de un mercado en cubos de ~`bucket_size_usd` de volumen.

    Ordena los trades cronologicamente y los va acumulando hasta que el volumen
    del cubo alcanza `bucket_size_usd`; entonces cierra el cubo y empieza otro.
    El ultimo cubo se incluye aunque quede por debajo del tamaño (parcial).

    Devuelve una lista de dicts con: bucket_seq, ts_open, ts_close, volume_usd,
    signed_imbalance (-1..+1), price_open, price_close (precios en espacio Yes).
    """
    ordered = sorted(trades_list, key=lambda t: int(t.get("timestamp", 0)))

    buckets: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_vol = 0.0
    seq = 0

    def close(bucket: list[dict[str, Any]]) -> None:
        nonlocal seq
        if not bucket:
            return
        prices = [(_yes_price(t)) for t in bucket]
        prices = [p for p in prices if p is not None]
        buckets.append({
            "bucket_seq": seq,
            "ts_open": int(bucket[0].get("timestamp", 0)),
            "ts_close": int(bucket[-1].get("timestamp", 0)),
            "volume_usd": sum(_trade_usd(t) for t in bucket),
            "signed_imbalance": signed_imbalance(bucket),
            "price_open": prices[0] if prices else None,
            "price_close": prices[-1] if prices else None,
        })
        seq += 1

    for t in ordered:
        current.append(t)
        current_vol += _trade_usd(t)
        if current_vol >= bucket_size_usd:
            close(current)
            current = []
            current_vol = 0.0
    close(current)  # ultimo cubo parcial

    logger.info(
        "bucket_trades %s: %d trades -> %d cubos de ~$%.0f",
        condition_id, len(ordered), len(buckets), bucket_size_usd,
    )
    return buckets


# --------------------------------------------------------------------------- #
# Persistencia
# --------------------------------------------------------------------------- #
def _iso(unix_ts: int | None) -> str | None:
    """Convierte un timestamp unix a ISO UTC (la tabla guarda ts como TEXT)."""
    if unix_ts is None:
        return None
    return datetime.fromtimestamp(int(unix_ts), timezone.utc).isoformat()


def connect(db_path: Any = DB_PATH) -> sqlite3.Connection:
    """Abre el SQLite del proyecto y garantiza la tabla volume_buckets."""
    conn = db.connect(db_path)
    conn.executescript(VOLUME_BUCKETS_SCHEMA)
    conn.commit()
    return conn


def save_buckets(conn: sqlite3.Connection, condition_id: str, buckets: list[dict[str, Any]]) -> None:
    """Inserta/reemplaza los cubos de un mercado (PK condition_id + bucket_seq)."""
    conn.executemany(
        "INSERT OR REPLACE INTO volume_buckets (condition_id, bucket_seq, ts_open, "
        "ts_close, volume_usd, signed_imbalance, price_open, price_close) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                condition_id, b["bucket_seq"], _iso(b["ts_open"]), _iso(b["ts_close"]),
                b["volume_usd"], b["signed_imbalance"], b["price_open"], b["price_close"],
            )
            for b in buckets
        ],
    )
    conn.commit()


def _main() -> None:
    """Demo: bucketiza los trades del mercado de politica de mayor volumen."""
    from src.client.data_api import get_market_trades
    from src.client.gamma import flatten_markets, get_politics_events

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    markets = [m for m in flatten_markets(get_politics_events()) if m.get("condition_id")]
    markets.sort(key=lambda m: m.get("volume_24h") or 0, reverse=True)
    cid = markets[0]["condition_id"]

    trades = get_market_trades(cid, limit=500)
    buckets = bucket_trades(cid, trades)
    conn = connect()
    save_buckets(conn, cid, buckets)
    conn.close()
    print(f"\n{len(buckets)} cubos guardados para {markets[0]['question']!r}")
    for b in buckets:
        print(f"  #{b['bucket_seq']} vol=${b['volume_usd']:,.0f} imbalance={b['signed_imbalance']:+.2f}")


if __name__ == "__main__":
    _main()

