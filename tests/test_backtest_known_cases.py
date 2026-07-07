"""Tests de la reconstruccion sin lookahead del backtest de casos conocidos."""
import unittest

from src.analysis.backtest_known_cases import (
    bucket_state_at,
    reconstruct_profile,
    reconstruct_streak,
    first_alert,
    ScoredTrade,
)
from src.analysis.scoring import ScoreBreakdown


def _t(ts, side="BUY", outcome="Yes", size=1000.0, price=0.1, cid="M") -> dict:
    return {"timestamp": ts, "side": side, "outcome": outcome, "size": size,
            "price": price, "conditionId": cid}


class TestReconstructProfile(unittest.TestCase):
    def test_solo_usa_trades_anteriores_al_cutoff(self) -> None:
        trades = [_t(100, size=1000, price=1.0), _t(200, size=2000, price=1.0),
                  _t(300, size=9999, price=1.0)]  # este es futuro respecto al cutoff 250
        cutoff = 250
        p = reconstruct_profile("w", trades, cutoff_ts=cutoff, funding_ts=cutoff - 3 * 86400)
        # solo cuentan los de ts<250 => volumen 1000+2000=3000, 1 mercado
        self.assertAlmostEqual(p.total_volume_usd, 3000.0)
        self.assertEqual(p.n_markets, 1)
        # edad respecto al cutoff (250), no a hoy
        self.assertAlmostEqual(p.wallet_age_days, 3.0, places=4)
        self.assertIsNone(p.win_rate)  # conservador, sin lookahead

    def test_sin_funding_edad_none(self) -> None:
        p = reconstruct_profile("w", [_t(100)], cutoff_ts=200, funding_ts=None)
        self.assertIsNone(p.wallet_age_days)


class TestBucketStateAt(unittest.TestCase):
    def test_ventana_no_disponible_devuelve_cero(self) -> None:
        market = [_t(1000), _t(1100)]
        self.assertEqual(bucket_state_at(market, ts=500, bucket_size_usd=5000), (0.0, 0.0))

    def test_acumula_hasta_ts(self) -> None:
        # dos compras Yes de $1000 antes de ts => imbalance +1, volumen 2000
        market = [_t(100, size=1000, price=1.0), _t(200, size=1000, price=1.0), _t(999, size=1000, price=1.0)]
        imb, vol = bucket_state_at(market, ts=200, bucket_size_usd=5000)
        self.assertAlmostEqual(imb, 1.0)
        self.assertAlmostEqual(vol, 2000.0)

    def test_reset_al_cerrar_cubo(self) -> None:
        # 3 trades de $2000 => 6000 >= 5000 cierra; el cubo abierto queda vacio (0)
        market = [_t(100, size=2000, price=1.0), _t(150, size=2000, price=1.0), _t(200, size=2000, price=1.0)]
        imb, vol = bucket_state_at(market, ts=200, bucket_size_usd=5000)
        self.assertEqual(vol, 0.0)  # se acaba de cerrar el cubo


class TestReconstructStreak(unittest.TestCase):
    def test_racha_mismo_lado_y_precio_peor(self) -> None:
        # tres compras Yes con precio creciente => racha 3, price_against True
        trades = [_t(1, price=0.10), _t(2, price=0.20), _t(3, price=0.30)]
        streak, against = reconstruct_streak(trades, idx=2)
        self.assertEqual(streak, 3)
        self.assertTrue(against)

    def test_cambio_de_lado_reinicia_racha(self) -> None:
        trades = [_t(1, outcome="Yes"), _t(2, outcome="No")]
        streak, _ = reconstruct_streak(trades, idx=1)
        self.assertEqual(streak, 1)


class TestFirstAlert(unittest.TestCase):
    def test_devuelve_primer_cruce(self) -> None:
        s = [ScoredTrade(1, "BUY", "Yes", 0.1, 100, ScoreBreakdown(score_total=30)),
             ScoredTrade(2, "BUY", "Yes", 0.1, 100, ScoreBreakdown(score_total=55)),
             ScoredTrade(3, "BUY", "Yes", 0.1, 100, ScoreBreakdown(score_total=80))]
        self.assertEqual(first_alert(s, threshold=50).ts, 2)

    def test_ninguno_cruza(self) -> None:
        s = [ScoredTrade(1, "BUY", "Yes", 0.1, 100, ScoreBreakdown(score_total=30))]
        self.assertIsNone(first_alert(s, threshold=50))


if __name__ == "__main__":
    unittest.main()
