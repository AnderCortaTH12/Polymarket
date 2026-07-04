"""Esquema SQLite del collector.

Define la tabla `snapshots`: una foto del estado de cada mercado en un instante.
La primary key (ts, market_id) permite reconstruir la evolucion completa de
cualquier mercado a lo largo del tiempo.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# La base de datos vive siempre en data/polymarket_politics.db (convencion).
DB_PATH: Path = Path(__file__).resolve().parents[2] / "data" / "polymarket_politics.db"

SCHEMA: str = """
CREATE TABLE IF NOT EXISTS snapshots (
    ts               TEXT NOT NULL,   -- ISO UTC del snapshot
    market_id        TEXT NOT NULL,
    condition_id     TEXT,            -- necesario para la Data API (FASE 5)
    event_slug       TEXT,
    question         TEXT,
    outcomes         TEXT,            -- JSON
    outcome_prices   TEXT,            -- JSON
    best_bid         REAL,
    best_ask         REAL,
    spread           REAL,
    last_trade_price REAL,
    volume_24h       REAL,
    volume_total     REAL,
    liquidity        REAL,
    end_date         TEXT,
    PRIMARY KEY (ts, market_id)
);
CREATE INDEX IF NOT EXISTS idx_market_ts ON snapshots (market_id, ts);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Abre (creando si hace falta) la base de datos y garantiza el esquema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    logger.debug("Conectado a %s", db_path)
    return conn
