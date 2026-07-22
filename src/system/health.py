"""Monitoreo de salud del sistema: uso de disco y alertas por Telegram.

Chequea periódicamente el uso del disco y envía alertas si supera umbrales,
con deduplicación para no spamear.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

DISK_ALERT_THRESHOLD: float = 0.85  # 85% de uso
DISK_ALERT_DEDUPE_HOURS: int = 6


def check_disk_usage(
    disk_path: str = "/",
    threshold: float = DISK_ALERT_THRESHOLD,
) -> float:
    """Devuelve el uso del disco como fracción (0.0 a 1.0).

    Usa shutil.disk_usage para obtener información del uso sin parsear df.
    """
    try:
        usage = shutil.disk_usage(disk_path)
        return usage.used / usage.total
    except Exception as exc:
        logger.warning("No se pudo obtener uso de disco: %s", exc)
        return 0.0


def should_alert_disk_usage(
    conn: sqlite3.Connection,
    current_usage: float,
    threshold: float = DISK_ALERT_THRESHOLD,
    dedupe_hours: int = DISK_ALERT_DEDUPE_HOURS,
) -> bool:
    """Determina si debe enviarse una alerta de disco.

    Retorna True si el uso > threshold y no hay una alerta reciente (en las
    últimas `dedupe_hours` horas) de este problema en la tabla service_health.
    """
    if current_usage <= threshold:
        return False

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=dedupe_hours)).isoformat()

    cursor = conn.execute(
        """SELECT COUNT(*) FROM service_health
           WHERE ts >= ? AND message LIKE ?""",
        (cutoff, "disk_usage>%")
    )
    recent_alerts = cursor.fetchone()[0]
    return recent_alerts == 0


def log_disk_alert(
    conn: sqlite3.Connection,
    usage_pct: float,
) -> None:
    """Registra una alerta de disco en service_health."""
    ts = datetime.now(timezone.utc).isoformat()
    message = f"disk_usage>{usage_pct*100:.1f}%"
    try:
        conn.execute(
            "INSERT INTO service_health (ts, service, status, message) VALUES (?, ?, ?, ?)",
            (ts, "disk_monitor", "warning", message)
        )
        conn.commit()
    except Exception as exc:
        logger.warning("No se pudo registrar alerta de disco: %s", exc)
