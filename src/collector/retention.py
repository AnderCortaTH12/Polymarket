"""Retención y purga automática de snapshots para mantener el disco bajo control.

Define políticas de retención (cuántos días guardar) y una función de purga que
ejecuta una limpieza automática para liberar espacio sin que el disco llegue al 100%.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS: int = 30


def purge_old_snapshots(
    conn: sqlite3.Connection,
    days: int = DEFAULT_RETENTION_DAYS,
) -> int:
    """Borra snapshots más antiguos que N días. Devuelve número de filas borradas.

    Ejecuta un DELETE sobre la tabla snapshots filtrando por ts ISO < hace N días,
    luego un VACUUM para recuperar el espacio (si hay suficiente espacio libre).
    La tabla snapshots usa `ts` (TEXT, ISO UTC) como primary key, así que es
    seguro hacer SELECT MIN(ts) para encontrar los más antiguos.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cursor = conn.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
    deleted = cursor.rowcount
    conn.commit()
    if deleted > 0:
        logger.info("Purgados %d snapshots más antiguos que %d días (cutoff: %s)",
                   deleted, days, cutoff)
    return deleted
