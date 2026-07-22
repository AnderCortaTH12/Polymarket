"""Sincronización de histórico de trades: backfill y refresco continuo.

Backfill: obtiene el histórico completo de trades de una wallet desde la Data API.
Refresco: se lanza automáticamente cuando se detecta una alerta nueva para una
wallet cuyo último sync está más antiguo que WALLET_TRADES_TTL_HOURS.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from src.client.data_api import get_user_trades
from src.analysis.wallet_trades import upsert_wallet_trade, ensure_wallet_trades_schema

logger = logging.getLogger(__name__)

# TTL del histórico de una wallet: si la última sincronización está
# más vieja que esto, se relanza el backfill en background.
WALLET_TRADES_TTL_HOURS: float = 24.0

# Tabla auxiliar para rastrear cuándo se sincronizó por última vez cada wallet.
WALLET_SYNC_METADATA_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS wallet_trades_sync (
    wallet TEXT PRIMARY KEY,
    last_sync_ts TEXT NOT NULL  -- ISO UTC del último backfill exitoso
);
"""


def ensure_wallet_sync_metadata_schema(conn: sqlite3.Connection) -> None:
    """Crea la tabla wallet_trades_sync si no existe."""
    conn.executescript(WALLET_SYNC_METADATA_SCHEMA)
    conn.commit()


def backfill_wallet(conn: sqlite3.Connection, wallet: str, max_trades: int | None = None) -> int:
    """Obtiene todos los trades de una wallet y los upsert en wallet_trades.

    Retorna el número de trades insertados (nuevos; los duplicados por transaction_hash
    se ignoran). Si ya existe un sync reciente, se puede saltear llamando a
    should_refetch_wallet primero.

    Args:
        conn: conexion SQLite para escribir los trades.
        wallet: proxy_wallet a sincronizar.
        max_trades: tope de trades a traer (None = todos, puede ser lento).
    """
    ensure_wallet_trades_schema(conn)
    ensure_wallet_sync_metadata_schema(conn)

    try:
        trades = get_user_trades(wallet, max_trades=max_trades)
        inserted = 0
        for trade in trades:
            condition_id = trade.get("conditionId")
            if not condition_id:
                continue
            try:
                size_usd = None
                size_shares = trade.get("size")
                price = trade.get("price")
                if size_shares is not None and price is not None:
                    try:
                        size_usd = float(size_shares) * float(price)
                    except (TypeError, ValueError):
                        pass

                upsert_wallet_trade(
                    conn,
                    wallet=wallet,
                    condition_id=condition_id,
                    transaction_hash=trade.get("transactionHash", ""),
                    ts=trade.get("timestamp", ""),
                    side=trade.get("outcome"),
                    trade_side=trade.get("side"),
                    price=trade.get("price"),
                    size_shares=size_shares,
                    size_usd=size_usd,
                    outcome_index=trade.get("outcomeIndex"),
                )
                inserted += 1
            except Exception:  # noqa: BLE001
                logger.warning("Error procesando trade de wallet %s", wallet, exc_info=True)
                continue

        # Registrar que se sincronizó exitosamente.
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO wallet_trades_sync (wallet, last_sync_ts) VALUES (?, ?)",
            (wallet, now)
        )
        conn.commit()
        logger.info("Backfill wallet=%s: %d trades nuevos", wallet, inserted)
        return inserted
    except Exception:  # noqa: BLE001
        logger.exception("Fallo backfill wallet=%s", wallet)
        return 0


def should_refetch_wallet(conn: sqlite3.Connection, wallet: str, ttl_hours: float = WALLET_TRADES_TTL_HOURS) -> bool:
    """True si la wallet no tiene sync reciente (o nunca se sincronizó).

    Se usa en el detector para decidir si lanzar backfill en background cuando
    se detecta una alerta nueva para una wallet.
    """
    cursor = conn.execute(
        "SELECT last_sync_ts FROM wallet_trades_sync WHERE wallet = ?",
        (wallet,)
    )
    row = cursor.fetchone()
    if row is None:
        return True  # nunca se sincronizó
    try:
        last_sync = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
        return last_sync < cutoff
    except (TypeError, ValueError):
        return True
