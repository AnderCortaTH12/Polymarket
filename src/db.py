"""Apertura centralizada de conexiones SQLite del proyecto.

Todas las conexiones (collector, stream, storage, dashboard) deben abrirse por
aqui para activar WAL (Write-Ahead Logging) y un busy_timeout. WAL permite
lectura y escritura concurrentes (el dashboard lee mientras el detector escribe
sin bloquearse), y el busy_timeout hace que, si aun asi hay un lock momentaneo,
la conexion espere en vez de colgarse o fallar.
"""
from __future__ import annotations

import sqlite3
from typing import Any

BUSY_TIMEOUT_MS: int = 5000  # esperar hasta 5s ante un lock, en vez de colgarse


def connect(db_path: Any) -> sqlite3.Connection:
    """Abre una conexion SQLite en modo WAL con busy_timeout.

    WAL es una propiedad persistente del fichero (basta activarla una vez), pero
    reafirmarla en cada conexion es inocuo; el busy_timeout si es por-conexion.
    En bases `:memory:` el modo WAL no aplica y SQLite lo ignora sin error.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=%d;" % BUSY_TIMEOUT_MS)
    return conn
