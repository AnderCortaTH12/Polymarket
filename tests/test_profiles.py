"""Tests de las funciones puras de perfilado de wallets (sin red)."""
import os
import tempfile
import unittest
from unittest.mock import patch

from src.analysis import profiles
from src.analysis.profiles import (
    PROFILE_MAX_POSITIONS,
    build_profile,
    compute_trade_stats,
    compute_win_stats,
    ensure_profiles_schema,
    load_profile,
    save_profile,
    WalletProfile,
    _age_days_from_ts,
)
from src.client.data_api import NEUTRAL_SORT_BY
from src import db


def _trade(cid: str, size: float, price: float, ts: int) -> dict:
    return {"conditionId": cid, "size": size, "price": price, "timestamp": ts}


class TestComputeTradeStats(unittest.TestCase):
    def test_volumen_mercados_y_media(self) -> None:
        trades = [
            _trade("A", 100, 0.5, 1000),   # $50
            _trade("A", 200, 0.5, 900),    # $100
            _trade("B", 100, 0.9, 1200),   # $90
        ]
        s = compute_trade_stats(trades)
        self.assertAlmostEqual(s["total_volume_usd"], 240.0)
        self.assertEqual(s["n_markets"], 2)
        self.assertAlmostEqual(s["avg_trade_size_usd"], 80.0)
        self.assertEqual(s["first_trade_ts"], 900)  # el mas antiguo

    def test_concentracion_hhi(self) -> None:
        # Todo en un mercado -> HHI = 1.0
        s = compute_trade_stats([_trade("A", 100, 1.0, 1), _trade("A", 100, 1.0, 2)])
        self.assertAlmostEqual(s["concentration"], 1.0)
        # 50/50 en dos mercados -> HHI = 0.5
        s2 = compute_trade_stats([_trade("A", 100, 1.0, 1), _trade("B", 100, 1.0, 2)])
        self.assertAlmostEqual(s2["concentration"], 0.5)

    def test_vacio_no_rompe(self) -> None:
        s = compute_trade_stats([])
        self.assertEqual(s["total_volume_usd"], 0.0)
        self.assertEqual(s["n_markets"], 0)
        self.assertIsNone(s["first_trade_ts"])


class TestComputeWinStats(unittest.TestCase):
    def test_win_rate_y_longshots(self) -> None:
        positions = [
            {"curPrice": 1.0, "avgPrice": 0.20},   # ganada, longshot
            {"curPrice": 0.99, "avgPrice": 0.60},  # ganada, no longshot
            {"curPrice": 0.0, "avgPrice": 0.40},   # perdida
            {"curPrice": 0.55, "avgPrice": 0.50},  # abierta (no cuenta)
            {"redeemable": True, "curPrice": 0.97, "avgPrice": 0.10},  # ganada (redeemable), longshot
        ]
        s = compute_win_stats(positions)
        self.assertEqual(s["n_resolved"], 4)
        self.assertAlmostEqual(s["win_rate"], 3 / 4)
        self.assertEqual(s["longshot_wins"], 2)

    def test_sin_resueltas_win_rate_none(self) -> None:
        s = compute_win_stats([{"curPrice": 0.5, "avgPrice": 0.5}])
        self.assertIsNone(s["win_rate"])
        self.assertEqual(s["n_resolved"], 0)

    def test_win_rate_reliable_siempre_false(self) -> None:
        # Fase 1c: el win_rate no es reconstruible desde /positions, asi que
        # compute_win_stats lo marca SIEMPRE como no fiable, gane lo que gane.
        positions = [
            {"curPrice": 1.0, "avgPrice": 0.20},
            {"curPrice": 0.99, "avgPrice": 0.60},
            {"curPrice": 0.0, "avgPrice": 0.40},
        ]
        self.assertFalse(compute_win_stats(positions)["win_rate_reliable"])
        self.assertFalse(compute_win_stats([])["win_rate_reliable"])


class TestBuildProfileSurvivorship(unittest.TestCase):
    """El win_rate no debe sufrir sesgo de supervivencia (Fase 1b)."""

    def _patches(self, positions):
        # build_profile hace 4 llamadas de red; las mockeamos todas.
        return (
            patch("src.analysis.profiles.get_user_trades", return_value=[]),
            patch("src.analysis.profiles.get_user_positions", return_value=positions),
            patch("src.analysis.profiles.get_first_token_transfers", return_value=[]),
            patch("src.analysis.profiles.get_first_transactions", return_value=[]),
        )

    def test_pide_orden_neutral_y_tope_alto(self) -> None:
        p_tr, p_pos, p_tk, p_tx = self._patches([])
        with p_tr, p_pos as mock_pos, p_tk, p_tx:
            build_profile("0xW")
        _, kwargs = mock_pos.call_args
        self.assertEqual(kwargs.get("sort_by"), NEUTRAL_SORT_BY)
        self.assertEqual(kwargs.get("max_positions"), PROFILE_MAX_POSITIONS)

    def test_truncamiento_marca_win_rate_no_fiable(self) -> None:
        # Devuelve EXACTAMENTE el tope => muestra truncada => no fiable.
        positions = [{"curPrice": 1.0, "avgPrice": 0.5}] * PROFILE_MAX_POSITIONS
        p_tr, p_pos, p_tk, p_tx = self._patches(positions)
        with p_tr, p_pos, p_tk, p_tx:
            prof = build_profile("0xW")
        self.assertFalse(prof.win_rate_reliable)

    def test_win_rate_nunca_fiable_aunque_no_truncado(self) -> None:
        # Fase 1c: /positions no ve el historial cerrado, asi que el win_rate es
        # estructuralmente inservible AUNQUE la muestra no venga truncada.
        positions = [{"curPrice": 1.0, "avgPrice": 0.5}, {"curPrice": 0.0, "avgPrice": 0.5}]
        p_tr, p_pos, p_tk, p_tx = self._patches(positions)
        with p_tr, p_pos, p_tk, p_tx:
            prof = build_profile("0xW")
        self.assertFalse(prof.win_rate_reliable)

    def test_resto_del_perfil_intacto_tras_desactivar_track_record(self) -> None:
        # Fase 1c solo invalida win_rate; el resto del perfil (edad, volumen,
        # mercados, concentracion, funder) sale de trades/Polygonscan y debe
        # seguir calculandose igual. Este test lo fija para que el cambio no
        # toque nada mas.
        trades = [
            {"size": 100, "price": 0.5, "conditionId": "0xA", "timestamp": 1_000_000},
            {"size": 200, "price": 0.5, "conditionId": "0xB", "timestamp": 2_000_000},
        ]
        transfers = [{"from": "0xFUNDER", "timeStamp": "1000000"}]
        patches = (
            patch("src.analysis.profiles.get_user_trades", return_value=trades),
            patch("src.analysis.profiles.get_user_positions", return_value=[]),
            patch("src.analysis.profiles.get_first_token_transfers", return_value=transfers),
            patch("src.analysis.profiles.get_first_transactions", return_value=[]),
        )
        with patches[0], patches[1], patches[2], patches[3]:
            prof = build_profile("0xW")
        self.assertAlmostEqual(prof.total_volume_usd, 100 * 0.5 + 200 * 0.5)
        self.assertEqual(prof.n_markets, 2)
        self.assertAlmostEqual(prof.avg_trade_size_usd, 75.0)
        self.assertGreater(prof.concentration, 0.0)
        self.assertEqual(prof.funding_cluster_id, "0xFUNDER")
        self.assertIsNotNone(prof.wallet_age_days)


class TestProfilesMigration(unittest.TestCase):
    def test_win_rate_reliable_idempotente(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            # Tabla vieja SIN la columna win_rate_reliable, con una fila.
            conn = db.connect(path)
            conn.executescript(
                "CREATE TABLE wallet_profiles (wallet TEXT PRIMARY KEY, win_rate REAL, updated_at TEXT);"
            )
            conn.execute("INSERT INTO wallet_profiles (wallet, win_rate) VALUES ('0xW', 0.99)")
            conn.commit()
            conn.close()

            conn = db.connect(path)
            ensure_profiles_schema(conn)  # migra
            cols = {r[1] for r in conn.execute("PRAGMA table_info(wallet_profiles)")}
            self.assertIn("win_rate_reliable", cols)
            # La fila vieja se marca como NO fiable (0).
            val = conn.execute("SELECT win_rate_reliable FROM wallet_profiles WHERE wallet='0xW'").fetchone()[0]
            self.assertEqual(val, 0)
            conn.close()

            # Idempotente: reejecutar no falla ni cambia nada.
            conn = db.connect(path)
            ensure_profiles_schema(conn)
            val2 = conn.execute("SELECT win_rate_reliable FROM wallet_profiles WHERE wallet='0xW'").fetchone()[0]
            self.assertEqual(val2, 0)
            conn.close()

    def test_roundtrip_conserva_win_rate_reliable(self) -> None:
        conn = profiles.connect(":memory:")
        save_profile(conn, WalletProfile(wallet="0xW", win_rate=0.9, win_rate_reliable=False))
        loaded = load_profile(conn, "0xW")
        self.assertFalse(loaded.win_rate_reliable)
        conn.close()


class TestAgeDays(unittest.TestCase):
    def test_edad_en_dias(self) -> None:
        now = 1_000_000 + 3 * 86400
        self.assertAlmostEqual(_age_days_from_ts(1_000_000, now=now), 3.0)

    def test_none_si_no_hay_ts(self) -> None:
        self.assertIsNone(_age_days_from_ts(None))


if __name__ == "__main__":
    unittest.main()
