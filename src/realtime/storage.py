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
    score_total         INTEGER,         -- score NORMALIZADO (0-100); decide la alerta (Fase 2)
    score_breakdown     TEXT,            -- JSON {componente: puntos}
    bucket_imbalance    REAL,
    username            TEXT,            -- name/pseudonym del trader (Polymarket)
    transaction_hash    TEXT,            -- referencia auditable on-chain
    market_slug         TEXT,            -- para el link a polymarket.com
    trade_side          TEXT,            -- BUY / SELL (el 'side' es el outcome)
    scoring_version     TEXT,            -- version del scoring que genero la alerta
    score_bruto         INTEGER,         -- suma de componentes sin normalizar (evidencia absoluta)
    techo_evaluable     INTEGER,         -- suma de pesos de los componentes evaluables del trade
    componentes_evaluables TEXT,         -- JSON [nombres] que entraron en el techo (auditoria)
    notified_at         TEXT             -- ISO UTC si se envio la notif. Telegram; NULL si se dedupeo
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
    # Fase 2 (defecto 2): normalizacion del score.
    "score_bruto": "INTEGER",
    "techo_evaluable": "INTEGER",
    "componentes_evaluables": "TEXT",
    # Anti-spam de notificaciones: momento en que se envio el Telegram (NULL si
    # se dedupeo o aun no se ha notificado).
    "notified_at": "TEXT",
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
    ws_connected      INTEGER,
    service           TEXT,            -- nombre del servicio (disco_monitor, etc)
    status            TEXT,            -- estado: ok, warning, error
    message           TEXT             -- mensaje de diagnostico
);
"""

_SERVICE_HEALTH_EXTRA_COLUMNS: dict[str, str] = {
    "service": "TEXT",
    "status": "TEXT",
    "message": "TEXT",
}


def _ensure_service_health_columns(conn: sqlite3.Connection) -> None:
    """Añade columnas nuevas a service_health si no existen."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(service_health)")}
    for col, decl in _SERVICE_HEALTH_EXTRA_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE service_health ADD COLUMN {col} {decl}")
    conn.commit()


def connect(db_path: Any = DB_PATH) -> sqlite3.Connection:
    """Abre el SQLite del proyecto y garantiza las tablas alerts y service_health."""
    conn = db.connect(db_path)
    conn.executescript(ALERTS_SCHEMA)
    conn.executescript(SERVICE_HEALTH_SCHEMA)
    conn.commit()
    _ensure_columns(conn)
    _ensure_service_health_columns(conn)
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

    `score` es el `ScoreBreakdown` de `compute_score`. `score_total` es el score
    NORMALIZADO (0-100), el que decidio la alerta; se guardan tambien `score_bruto`
    (suma sin normalizar, evidencia absoluta), `techo_evaluable` y
    `componentes_evaluables` (JSON) para auditar la normalizacion, y los
    componentes en `score_breakdown` (JSON) para ajustar pesos despues. `username`,
    `transaction_hash`, `market_slug` y `trade_side` quedan como referencia auditable.
    """
    cur = conn.execute(
        "INSERT INTO alerts (ts, condition_id, market_question, wallet, side, "
        "trade_size_usd, price_at_detection, score_total, score_breakdown, bucket_imbalance, "
        "username, transaction_hash, market_slug, trade_side, scoring_version, "
        "score_bruto, techo_evaluable, componentes_evaluables) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts, condition_id, market_question, wallet, side,
            trade_size_usd, price_at_detection, score.score_total,
            json.dumps(score.components()), bucket_imbalance,
            username, transaction_hash, market_slug, trade_side, config.SCORING_VERSION,
            score.score_bruto, score.techo_evaluable,
            json.dumps(score.componentes_evaluables),
        ),
    )
    conn.commit()
    alert_id = int(cur.lastrowid)
    logger.info(
        "Alerta %d guardada: %s score=%d (bruto=%d/techo=%d) wallet=%s",
        alert_id, condition_id, score.score_total, score.score_bruto,
        score.techo_evaluable, wallet,
    )
    return alert_id


def recent_notification_exists(
    conn: sqlite3.Connection, wallet: str, condition_id: str, cutoff_iso: str
) -> bool:
    """True si ya se notifico esta (wallet, condition_id) desde `cutoff_iso`.

    Base del anti-spam: mira si alguna alerta de la MISMA wallet y MISMO mercado
    tiene `notified_at` (envio real) dentro de la ventana. Las alertas se guardan
    con timestamps ISO UTC homogeneos (offset +00:00), por lo que la comparacion
    lexicografica `>=` equivale a la temporal.
    """
    row = conn.execute(
        "SELECT 1 FROM alerts WHERE wallet = ? AND condition_id = ? "
        "AND notified_at IS NOT NULL AND notified_at >= ? LIMIT 1",
        (wallet, condition_id, cutoff_iso),
    ).fetchone()
    return row is not None


def mark_notified(conn: sqlite3.Connection, alert_id: int, notified_at: str) -> None:
    """Marca una alerta como notificada (Telegram enviado) con su timestamp ISO UTC."""
    conn.execute("UPDATE alerts SET notified_at = ? WHERE id = ?", (notified_at, alert_id))
    conn.commit()
