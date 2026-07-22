"""Tests de la Fase 1: migracion de `scoring_version` y filtro del backtest.

Cubre que la migracion es idempotente, que las filas preexistentes quedan como
"v1", que save_alert escribe la version vigente (v2) y que el backtest excluye
las v1 por defecto.
"""
import os
import tempfile
import unittest
import unittest.mock

from src import config, db
from src.analysis.scoring import ScoreBreakdown
from src.realtime import storage


# Esquema viejo de `alerts` SIN la columna scoring_version, para simular una BD
# que se creo antes de la migracion.
_OLD_ALERTS_SCHEMA = """
CREATE TABLE alerts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,
    condition_id        TEXT,
    market_question     TEXT,
    wallet              TEXT,
    side                TEXT,
    trade_size_usd      REAL,
    price_at_detection  REAL,
    score_total         INTEGER,
    score_breakdown     TEXT,
    bucket_imbalance    REAL,
    username            TEXT,
    transaction_hash    TEXT,
    market_slug         TEXT,
    trade_side          TEXT
);
"""


class TestScoringVersionMigration(unittest.TestCase):
    def _old_db(self, path: str) -> None:
        conn = db.connect(path)
        conn.executescript(_OLD_ALERTS_SCHEMA)
        conn.execute("INSERT INTO alerts (ts, score_total) VALUES ('2026-01-01T00:00:00+00:00', 50)")
        conn.execute("INSERT INTO alerts (ts, score_total) VALUES ('2026-01-02T00:00:00+00:00', 60)")
        conn.commit()
        conn.close()

    def test_migracion_marca_viejas_como_v1_e_idempotente(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            self._old_db(path)

            conn = storage.connect(path)  # aplica la migracion
            cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}
            self.assertIn("scoring_version", cols)
            versions = [r[0] for r in conn.execute("SELECT scoring_version FROM alerts ORDER BY id")]
            self.assertEqual(versions, ["v1", "v1"])
            conn.close()

            # Idempotente: reconectar no debe fallar ni cambiar nada.
            conn2 = storage.connect(path)
            versions2 = [r[0] for r in conn2.execute("SELECT scoring_version FROM alerts ORDER BY id")]
            self.assertEqual(versions2, ["v1", "v1"])
            conn2.close()

    def test_save_alert_escribe_version_vigente(self) -> None:
        conn = storage.connect(":memory:")
        aid = storage.save_alert(
            conn, ts="2026-07-01T00:00:00+00:00", condition_id="0xC", market_question="M",
            wallet="0xW", side="Yes", trade_size_usd=1000.0, price_at_detection=0.5,
            score=ScoreBreakdown(score_total=55),
        )
        version = conn.execute("SELECT scoring_version FROM alerts WHERE id=?", (aid,)).fetchone()[0]
        self.assertEqual(version, config.SCORING_VERSION)
        self.assertEqual(version, "v6")
        conn.close()

    def test_save_alert_guarda_los_cuatro_campos_de_normalizacion(self) -> None:
        conn = storage.connect(":memory:")
        score = ScoreBreakdown(
            tamano_anomalo=15, flujo_toxico=15, score_bruto=30, techo_evaluable=75,
            score_normalizado=40, componentes_evaluables=["wallet_fresca", "tamano_anomalo",
                                                           "concentracion", "flujo_toxico"],
            score_total=40,
        )
        aid = storage.save_alert(
            conn, ts="2026-07-01T00:00:00+00:00", condition_id="0xC", market_question="M",
            wallet="0xW", side="Yes", trade_size_usd=12000.0, price_at_detection=0.5, score=score,
        )
        row = conn.execute(
            "SELECT score_total, score_bruto, techo_evaluable, componentes_evaluables, "
            "scoring_version FROM alerts WHERE id=?", (aid,)
        ).fetchone()
        self.assertEqual(row[0], 40)   # score_total = normalizado
        self.assertEqual(row[1], 30)   # bruto
        self.assertEqual(row[2], 75)   # techo
        self.assertEqual(__import__("json").loads(row[3]),
                         ["wallet_fresca", "tamano_anomalo", "concentracion", "flujo_toxico"])
        self.assertEqual(row[4], "v6")
        conn.close()


class TestBacktestExcludesLegacy(unittest.TestCase):
    def test_load_alerts_excluye_v1_a_v4_por_defecto(self) -> None:
        from src import backtest_runner

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            conn = storage.connect(path)
            conn.execute(
                "INSERT INTO alerts (ts, score_total, price_at_detection, scoring_version) "
                "VALUES ('2026-01-01T00:00:00+00:00', 50, 0.5, 'v1')"
            )
            conn.execute(
                "INSERT INTO alerts (ts, score_total, price_at_detection, scoring_version) "
                "VALUES ('2026-01-02T00:00:00+00:00', 60, 0.5, 'v2')"
            )
            conn.execute(
                "INSERT INTO alerts (ts, score_total, price_at_detection, scoring_version) "
                "VALUES ('2026-01-03T00:00:00+00:00', 65, 0.5, 'v3')"
            )
            conn.execute(
                "INSERT INTO alerts (ts, score_total, price_at_detection, scoring_version) "
                "VALUES ('2026-01-04T00:00:00+00:00', 68, 0.5, 'v4')"
            )
            conn.execute(
                "INSERT INTO alerts (ts, score_total, price_at_detection, scoring_version) "
                "VALUES ('2026-01-05T00:00:00+00:00', 72, 0.5, 'v5')"
            )
            conn.execute(  # NULL cuenta como legacy (v1)
                "INSERT INTO alerts (ts, score_total, price_at_detection) "
                "VALUES ('2026-01-06T00:00:00+00:00', 70, 0.5)"
            )
            conn.commit()
            conn.close()

            with unittest.mock.patch.object(backtest_runner, "DB_PATH", path):
                df, excluded = backtest_runner.load_alerts()
            self.assertEqual(len(df), 1)
            self.assertEqual(int(df.iloc[0]["score_total"]), 72)  # solo la v5
            self.assertEqual(excluded, 5)  # v1, v2, v3, v4 y la NULL

            with unittest.mock.patch.object(backtest_runner, "DB_PATH", path):
                df_all, excluded_all = backtest_runner.load_alerts(scoring_version=None)
            self.assertEqual(len(df_all), 6)
            self.assertEqual(excluded_all, 0)


if __name__ == "__main__":
    unittest.main()
