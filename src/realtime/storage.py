"""Persistencia de la capa rapida de la Fase 6: tabla `alerts`.

Guarda cada deteccion con TODO el contexto y el score desglosado en JSON. Las
alertas NUNCA se borran: son el dataset del backtest. El servicio WebSocket
(paso 3b) usara `save_alert` desde el camino de scoring.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from src import config, db
from src.analysis.scoring import ScoreBreakdown
from src.collector.models import DB_PATH

logger = logging.getLogger(__name__)

ALERTS_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS alerts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,   -- momento de la DETECCION (clave para el backtest)
    condition_id        TEXT,
    market_question     TEXT,
    wallet              TEXT,
    side                TEXT,            -- outcome comprado (Yes/No u otro)
    trade_size_usd      REAL,
    price_at_detection  REAL,            -- precio del outcome al detectar
    score_total         INTEGER,
    score_breakdown     TEXT,            -- JSON {componente: puntos}
    bucket_imbalance    REAL,
    username            TEXT,            -- name/pseudonym del trader (Polymarket)
    transaction_hash    TEXT,            -- referencia auditable on-chain
    market_slug         TEXT,            -- para el link a polymarket.com
    trade_side          TEXT,            -- BUY / SELL (el 'side' es el outcome)
    scoring_version     TEXT             -- version del scoring que genero la alerta
);
"""

# Columnas añadidas despues de la creacion original de la tabla; se migran con
# ALTER para BDs que ya existian sin ellas.
_ALERTS_EXTRA_COLUMNS: dict[str, str] = {
    "username": "TEXT",
    "transaction_hash": "TEXT",
    "market_slug": "TEXT",
    "trade_side": "TEXT",
    "scoring_version": "TEXT",
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Añade columnas nuevas a `alerts` si la tabla se creo con un esquema viejo.

    Idempotente: solo hace ALTER de las que faltan. Las filas que ya existian se
    quedan con scoring_version NULL; se marcan como "v1" (perfilado inactivo) para
    que el backtest pueda excluirlas de las nuevas (v2).
    """
    existing = {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}
    added_scoring_version = "scoring_version" not in existing
    for col, decl in _ALERTS_EXTRA_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {decl}")
    if added_scoring_version:
        # Las alertas preexistentes son del scoring defectuoso: etiquetar como v1.
        conn.execute("UPDATE alerts SET scoring_version = 'v1' WHERE scoring_version IS NULL")
    conn.commit()


SERVICE_HEALTH_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS service_health (
    ts                TEXT PRIMARY KEY,
    trades_processed  INTEGER,
    ws_connected      INTEGER
);
"""


def connect(db_path: Any = DB_PATH) -> sqlite3.Connection:
    """Abre el SQLite del proyecto y garantiza las tablas alerts y service_health."""
    conn = db.connect(db_path)
    conn.executescript(ALERTS_SCHEMA)
    conn.executescript(SERVICE_HEALTH_SCHEMA)
    conn.commit()
    _ensure_columns(conn)
    return conn


def save_heartbeat(conn: sqlite3.Connection, ts: str, trades_processed: int, ws_connected: bool) -> None:
    """Escribe un latido del detector para que el dashboard sepa si esta vivo."""
    conn.execute(
        "INSERT OR REPLACE INTO service_health (ts, trades_processed, ws_connected) VALUES (?, ?, ?)",
        (ts, trades_processed, 1 if ws_connected else 0),
    )
    conn.commit()


def save_alert(
    conn: sqlite3.Connection,
    ts: str,
    condition_id: str,
    market_question: str,
    wallet: str,
    side: str,
    trade_size_usd: float,
    price_at_detection: float,
    score: ScoreBreakdown,
    bucket_imbalance: float | None = None,
    username: str | None = None,
    transaction_hash: str | None = None,
    market_slug: str | None = None,
    trade_side: str | None = None,
) -> int:
    """Inserta una alerta con el score desglosado en JSON. Devuelve su id.

    `score` es el `ScoreBreakdown` de `compute_score`; se guarda el total en
    `score_total` y los componentes en `score_breakdown` (JSON) para poder
    ajustar pesos despues sin perder informacion. `username`, `transaction_hash`,
    `market_slug` y `trade_side` quedan como referencia legible y auditable.
    """
    cur = conn.execute(
        "INSERT INTO alerts (ts, condition_id, market_question, wallet, side, "
        "trade_size_usd, price_at_detection, score_total, score_breakdown, bucket_imbalance, "
        "username, transaction_hash, market_slug, trade_side, scoring_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts, condition_id, market_question, wallet, side,
            trade_size_usd, price_at_detection, score.score_total,
            json.dumps(score.components()), bucket_imbalance,
            username, transaction_hash, market_slug, trade_side, config.SCORING_VERSION,
        ),
    )
    conn.commit()
    alert_id = int(cur.lastrowid)
    logger.info("Alerta %d guardada: %s score=%d wallet=%s", alert_id, condition_id, score.score_total, wallet)
    return alert_id
