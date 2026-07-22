"""Tests para el monitoreo de salud del sistema (disco, alertas)."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src import db
from src.system.health import (
    check_disk_usage,
    should_alert_disk_usage,
    log_disk_alert,
    DISK_ALERT_THRESHOLD,
    DISK_ALERT_DEDUPE_HOURS,
)

SERVICE_HEALTH_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS service_health (
    ts                TEXT PRIMARY KEY,
    trades_processed  INTEGER,
    ws_connected      INTEGER,
    service           TEXT,
    status            TEXT,
    message           TEXT
);
"""


class TestCheckDiskUsage(unittest.TestCase):
    """Verifica que check_disk_usage devuelve un valor razonable."""

    def test_devuelve_fraccion_entre_0_y_1(self) -> None:
        """El uso debe estar entre 0.0 y 1.0."""
        usage = check_disk_usage()
        self.assertGreaterEqual(usage, 0.0)
        self.assertLessEqual(usage, 1.0)

    @patch("src.system.health.shutil.disk_usage")
    def test_calcula_uso_correctamente(self, mock_disk_usage) -> None:
        """Verifica que calcula used/total correctamente."""
        from collections import namedtuple
        Usage = namedtuple("Usage", ["total", "used", "free"])
        mock_disk_usage.return_value = Usage(total=1000, used=500, free=500)

        usage = check_disk_usage()
        self.assertEqual(usage, 0.5)

    @patch("src.system.health.shutil.disk_usage", side_effect=Exception("error"))
    def test_maneja_excepciones(self, _mock_disk_usage) -> None:
        """Si falla disk_usage, devuelve 0.0."""
        usage = check_disk_usage()
        self.assertEqual(usage, 0.0)


class TestShouldAlertDiskUsage(unittest.TestCase):
    """Verifica la lógica de deduplicación de alertas de disco."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(SERVICE_HEALTH_SCHEMA)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmpdir.cleanup()

    def test_alerta_si_uso_supera_threshold(self) -> None:
        """Debe alertar si el uso > threshold."""
        result = should_alert_disk_usage(self.conn, 0.90, threshold=0.85)
        self.assertTrue(result)

    def test_no_alerta_si_uso_bajo_threshold(self) -> None:
        """No debe alertar si el uso <= threshold."""
        result = should_alert_disk_usage(self.conn, 0.80, threshold=0.85)
        self.assertFalse(result)

    def test_deduplica_alertas_recientes(self) -> None:
        """No debe alertar si hay una alerta reciente del mismo problema."""
        # Inserta una alerta reciente.
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "INSERT INTO service_health (ts, service, status, message) VALUES (?, ?, ?, ?)",
            (now.isoformat(), "disk_monitor", "warning", "disk_usage>87.5%")
        )
        self.conn.commit()

        # No debe alertar nuevamente (está deduplicada).
        result = should_alert_disk_usage(self.conn, 0.90, threshold=0.85, dedupe_hours=6)
        self.assertFalse(result)

    def test_alerta_si_anterior_fuera_de_ventana_dedupe(self) -> None:
        """Debe alertar si la alerta anterior está fuera de la ventana de dedupe."""
        # Inserta una alerta vieja (9 horas atrás).
        old = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
        self.conn.execute(
            "INSERT INTO service_health (ts, service, status, message) VALUES (?, ?, ?, ?)",
            (old, "disk_monitor", "warning", "disk_usage>87.5%")
        )
        self.conn.commit()

        # Debe alertar nuevamente.
        result = should_alert_disk_usage(self.conn, 0.90, threshold=0.85, dedupe_hours=6)
        self.assertTrue(result)


class TestLogDiskAlert(unittest.TestCase):
    """Verifica que log_disk_alert registra correctamente."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(SERVICE_HEALTH_SCHEMA)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmpdir.cleanup()

    def test_registra_alerta_en_service_health(self) -> None:
        """Verifica que la alerta se registra en la BD."""
        log_disk_alert(self.conn, 0.87)

        cursor = self.conn.execute("SELECT COUNT(*) FROM service_health")
        count = cursor.fetchone()[0]
        self.assertEqual(count, 1)

        cursor = self.conn.execute("SELECT service, status, message FROM service_health")
        service, status, message = cursor.fetchone()
        self.assertEqual(service, "disk_monitor")
        self.assertEqual(status, "warning")
        self.assertIn("87.0%", message)


if __name__ == "__main__":
    unittest.main()
