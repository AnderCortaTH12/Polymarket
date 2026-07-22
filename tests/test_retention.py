"""Tests para la retención y purga de snapshots."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.collector.models import SCHEMA
from src.collector.retention import purge_old_snapshots, DEFAULT_RETENTION_DAYS


class TestPurgeOldSnapshots(unittest.TestCase):
    """Verifica que purge_old_snapshots borra filas viejas y mantiene las recientes."""

    def setUp(self) -> None:
        """Crea una BD temporal para los tests."""
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def tearDown(self) -> None:
        """Cierra la BD y limpia."""
        self.conn.close()
        self.tmpdir.cleanup()

    def _insert_snapshot(self, ts: str) -> None:
        """Helper para insertar un snapshot con timestamp dado."""
        self.conn.execute(
            "INSERT INTO snapshots (ts, market_id, condition_id) VALUES (?, ?, ?)",
            (ts, "market_" + ts[:10], "cond_" + ts[:10])
        )
        self.conn.commit()

    def test_borra_snapshots_viejos(self) -> None:
        """Verifica que solo se borran snapshots más antiguos que el cutoff."""
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=40)).isoformat()      # borrar
        recent = (now - timedelta(days=10)).isoformat()   # mantener

        self._insert_snapshot(old)
        self._insert_snapshot(recent)

        deleted = purge_old_snapshots(self.conn, days=30)
        self.assertEqual(deleted, 1)

        cursor = self.conn.execute("SELECT COUNT(*) FROM snapshots")
        remaining = cursor.fetchone()[0]
        self.assertEqual(remaining, 1)

    def test_no_borra_si_todo_es_reciente(self) -> None:
        """Verifica que no borra nada si todos los snapshots son recientes."""
        now = datetime.now(timezone.utc)
        recent1 = (now - timedelta(days=5)).isoformat()
        recent2 = (now - timedelta(days=15)).isoformat()

        self._insert_snapshot(recent1)
        self._insert_snapshot(recent2)

        deleted = purge_old_snapshots(self.conn, days=30)
        self.assertEqual(deleted, 0)

        cursor = self.conn.execute("SELECT COUNT(*) FROM snapshots")
        remaining = cursor.fetchone()[0]
        self.assertEqual(remaining, 2)

    def test_respeta_parametro_dias(self) -> None:
        """Verifica que el parámetro `days` funciona correctamente."""
        now = datetime.now(timezone.utc)
        very_old = (now - timedelta(days=100)).isoformat()
        old = (now - timedelta(days=40)).isoformat()
        recent = (now - timedelta(days=10)).isoformat()

        self._insert_snapshot(very_old)
        self._insert_snapshot(old)
        self._insert_snapshot(recent)

        deleted = purge_old_snapshots(self.conn, days=30)
        self.assertEqual(deleted, 2)  # borró both very_old y old

        cursor = self.conn.execute("SELECT COUNT(*) FROM snapshots")
        remaining = cursor.fetchone()[0]
        self.assertEqual(remaining, 1)


if __name__ == "__main__":
    unittest.main()
