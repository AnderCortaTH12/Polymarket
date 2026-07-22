"""Histórico de trades por wallet para detectar salidas y reconstruir win_rate real.

Tabla inmutable de trades desde la Data API: cada trade se guarda una sola vez
(by transaction_hash) y permite auditar el comportamiento histórico de una wallet,
detectar cuándo salió de un mercado (BALLENA_VENDE) y calcular un win_rate desde
los TRADES + resolucion real (no desde posiciones abiertas, que tienen sesgo de
supervivencia).
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from src import db

logger = logging.getLogger(__name__)

WALLET_TRADES_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS wallet_trades (
    wallet          TEXT NOT NULL,
    condition_id    TEXT NOT NULL,
    transaction_hash TEXT NOT NULL,
    ts              TEXT NOT NULL,            -- ISO UTC
    side            TEXT,                     -- outcome (Yes/No)
    trade_side      TEXT,                     -- BUY/SELL
    price           REAL,
    size_shares     REAL,
    size_usd        REAL,
    outcome_index   INTEGER,
    PRIMARY KEY (transaction_hash, wallet)
);
CREATE INDEX IF NOT EXISTS idx_wallet_trades_wallet ON wallet_trades (wallet, ts);
CREATE INDEX IF NOT EXISTS idx_wallet_trades_market ON wallet_trades (condition_id, ts);
"""


def ensure_wallet_trades_schema(conn: sqlite3.Connection) -> None:
    """Crea la tabla wallet_trades si no existe."""
    conn.executescript(WALLET_TRADES_SCHEMA)
    conn.commit()


def upsert_wallet_trade(
    conn: sqlite3.Connection,
    wallet: str,
    condition_id: str,
    transaction_hash: str,
    ts: str,
    side: str | None,
    trade_side: str | None,
    price: float | None,
    size_shares: float | None,
    size_usd: float | None,
    outcome_index: int | None,
) -> None:
    """Inserta o ignora un trade de una wallet (idempotente por transaction_hash)."""
    conn.execute(
        """INSERT OR IGNORE INTO wallet_trades
           (wallet, condition_id, transaction_hash, ts, side, trade_side, price,
            size_shares, size_usd, outcome_index)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (wallet, condition_id, transaction_hash, ts, side, trade_side, price,
         size_shares, size_usd, outcome_index)
    )
    conn.commit()


def get_wallet_exit(
    conn: sqlite3.Connection,
    wallet: str,
    condition_id: str,
    after_ts: str,
) -> dict[str, Any] | None:
    """Devuelve el primer SELL de una wallet en un mercado después de after_ts.

    Retorna el dict del trade (con todos sus campos) o None si no hay SELL posterior.
    Se usa para detectar cuándo la ballena que activó una alerta salió de la posición.
    """
    cursor = conn.execute(
        """SELECT wallet, condition_id, transaction_hash, ts, side, trade_side, price,
                  size_shares, size_usd, outcome_index
           FROM wallet_trades
           WHERE wallet = ? AND condition_id = ? AND trade_side = 'SELL' AND ts > ?
           ORDER BY ts ASC
           LIMIT 1""",
        (wallet, condition_id, after_ts)
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        "wallet": row[0],
        "condition_id": row[1],
        "transaction_hash": row[2],
        "ts": row[3],
        "side": row[4],
        "trade_side": row[5],
        "price": row[6],
        "size_shares": row[7],
        "size_usd": row[8],
        "outcome_index": row[9],
    }
