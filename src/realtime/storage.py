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
    bucket_imbalance    REAL
);
"""


def connect(db_path: Any = DB_PATH) -> sqlite3.Connection:
    """Abre el SQLite del proyecto y garantiza la tabla alerts."""
    conn = sqlite3.connect(db_path)
    conn.executescript(ALERTS_SCHEMA)
    conn.commit()
    return conn


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
) -> int:
    """Inserta una alerta con el score desglosado en JSON. Devuelve su id.

    `score` es el `ScoreBreakdown` de `compute_score`; se guarda el total en
    `score_total` y los componentes en `score_breakdown` (JSON) para poder
    ajustar pesos despues sin perder informacion.
    """
    cur = conn.execute(
        "INSERT INTO alerts (ts, condition_id, market_question, wallet, side, "
        "trade_size_usd, price_at_detection, score_total, score_breakdown, bucket_imbalance) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts, condition_id, market_question, wallet, side,
            trade_size_usd, price_at_detection, score.score_total,
            json.dumps(score.components()), bucket_imbalance,
        ),
    )
    conn.commit()
    alert_id = int(cur.lastrowid)
    logger.info("Alerta %d guardada: %s score=%d wallet=%s", alert_id, condition_id, score.score_total, wallet)
    return alert_id
