"""Test de la apertura centralizada de conexiones (WAL + busy_timeout)."""
import tempfile
import unittest
from pathlib import Path

from src import db


class TestConnect(unittest.TestCase):
    def test_wal_y_busy_timeout_en_fichero(self) -> None:
        # WAL es propiedad del fichero; en :memory: no aplica, asi que usamos disco.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.db"
            conn = db.connect(path)
            try:
                mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
                timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(mode.lower(), "wal")
            self.assertEqual(timeout, db.BUSY_TIMEOUT_MS)

    def test_dos_conexiones_concurrentes_lectura_escritura(self) -> None:
        # Con WAL un lector y un escritor pueden coexistir sin bloquearse.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.db"
            writer = db.connect(path)
            writer.execute("CREATE TABLE t (x INTEGER)")
            writer.execute("INSERT INTO t VALUES (1)")
            writer.commit()

            reader = db.connect(path)
            writer.execute("INSERT INTO t VALUES (2)")  # escritura abierta
            # el lector puede leer el ultimo commit sin esperar al escritor
            n = reader.execute("SELECT COUNT(*) FROM t").fetchone()[0]
            self.assertEqual(n, 1)  # ve el commit previo, no la insercion sin commit
            writer.commit()
            reader.close()
            writer.close()


if __name__ == "__main__":
    unittest.main()
