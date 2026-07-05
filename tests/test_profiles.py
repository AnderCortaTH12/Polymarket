"""Tests de las funciones puras de perfilado de wallets (sin red)."""
import unittest

from src.analysis.profiles import (
    compute_trade_stats,
    compute_win_stats,
    _age_days_from_ts,
)


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


class TestAgeDays(unittest.TestCase):
    def test_edad_en_dias(self) -> None:
        now = 1_000_000 + 3 * 86400
        self.assertAlmostEqual(_age_days_from_ts(1_000_000, now=now), 3.0)

    def test_none_si_no_hay_ts(self) -> None:
        self.assertIsNone(_age_days_from_ts(None))


if __name__ == "__main__":
    unittest.main()
