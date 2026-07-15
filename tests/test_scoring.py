"""Tests del scoring compuesto (funcion pura) y de save_alert.

10 casos inventados que verifican el total y los componentes, mockeando el
perfil de wallet con un objeto simple. Sin red.
"""
import sqlite3
import unittest
from dataclasses import dataclass

from src import config
from src.analysis.scoring import ScoreBreakdown, compute_score
from src.realtime.storage import connect, save_alert
import json


@dataclass
class FakeProfile:
    """Perfil minimo para el scorer (mismos atributos que WalletProfile)."""
    wallet_age_days: float | None = 100.0
    win_rate: float | None = None
    n_resolved: int = 0
    funding_cluster_id: str | None = None
    concentration: float = 0.0
    total_volume_usd: float = 0.0
    win_rate_reliable: bool = True


def _trade(side="BUY", outcome="Yes", size=100.0, price=0.5) -> dict:
    return {"side": side, "outcome": outcome, "size": size, "price": price, "conditionId": "cid"}


W = config.SCORE_WEIGHTS


class TestComputeScore(unittest.TestCase):
    def test_1_wallet_vieja_trade_pequeno_cero(self) -> None:
        s = compute_score(_trade(size=100, price=0.5), FakeProfile(wallet_age_days=300), 0.0)
        self.assertEqual(s.score_total, 0)

    def test_2_wallet_fresca(self) -> None:
        s = compute_score(_trade(), FakeProfile(wallet_age_days=5), 0.0)
        self.assertEqual(s.wallet_fresca, W["wallet_fresca"])
        self.assertEqual(s.score_total, W["wallet_fresca"])

    def test_3_wallet_muy_fresca_suma_extra(self) -> None:
        s = compute_score(_trade(), FakeProfile(wallet_age_days=1), 0.0)
        self.assertEqual(s.wallet_fresca, W["wallet_fresca"] + W["wallet_fresca_extra"])

    def test_4_sin_perfil_trade_grande(self) -> None:
        # trade $6000 (size 12000 * 0.5), sin perfil
        s = compute_score(_trade(size=12000, price=0.5), None, 0.0)
        self.assertEqual(s.sin_perfil, W["sin_perfil"])

    def test_5_sin_perfil_trade_pequeno_no_suma(self) -> None:
        s = compute_score(_trade(size=100, price=0.5), None, 0.0)
        self.assertEqual(s.sin_perfil, 0)

    def test_6_tamano_anomalo(self) -> None:
        # trade $15000 (size 30000 * 0.5)
        s = compute_score(_trade(size=30000, price=0.5), FakeProfile(wallet_age_days=300), 0.0)
        self.assertEqual(s.tamano_anomalo, W["tamano_anomalo"])

    def test_7_longshot_con_conviccion(self) -> None:
        # BUY a prob 0.10, trade $5000 (size 50000 * 0.10)
        s = compute_score(_trade(size=50000, price=0.10), FakeProfile(wallet_age_days=300), 0.0)
        self.assertEqual(s.longshot, W["longshot"])
        # vender un longshot NO cuenta
        s2 = compute_score(_trade(side="SELL", size=50000, price=0.10), FakeProfile(wallet_age_days=300), 0.0)
        self.assertEqual(s2.longshot, 0)


class TestRelevanceFloor(unittest.TestCase):
    """Fase 2: relevance_floor devuelve el suelo del tramo correcto por precio."""

    def test_bordes_de_tramo(self) -> None:
        self.assertEqual(config.relevance_floor(0.05), 500.0)
        self.assertEqual(config.relevance_floor(0.10), 500.0)    # borde inferior inclusivo
        self.assertEqual(config.relevance_floor(0.101), 1500.0)  # justo por encima
        self.assertEqual(config.relevance_floor(0.20), 1500.0)
        self.assertEqual(config.relevance_floor(0.35), 1500.0)
        self.assertEqual(config.relevance_floor(0.351), 3000.0)  # sale del tramo longshot
        self.assertEqual(config.relevance_floor(0.50), 3000.0)
        self.assertEqual(config.relevance_floor(0.99), 3000.0)

    def test_precio_none_usa_el_tramo_mas_exigente(self) -> None:
        self.assertEqual(config.relevance_floor(None), 3000.0)

    def test_veto_y_longshot_comparten_suelo(self) -> None:
        # El mismo suelo gobierna veto y longshot: en 0.30, ambos usan $1.500.
        floor = config.relevance_floor(0.30)
        self.assertEqual(floor, 1500.0)
        below = compute_score(_trade(size=(floor - 1) / 0.30, price=0.30),
                              FakeProfile(wallet_age_days=300), 0.0)
        atfloor = compute_score(_trade(size=floor / 0.30, price=0.30),
                               FakeProfile(wallet_age_days=300), 0.0)
        self.assertEqual(below.longshot, 0)
        self.assertEqual(atfloor.longshot, W["longshot"])


class TestLongshotTiers(unittest.TestCase):
    """El minimo en $ del longshot escala con lo extremo del precio."""

    PROF = FakeProfile(wallet_age_days=300)

    def _score(self, usd: float, price: float):
        # size tal que size*price = usd
        return compute_score(_trade(size=usd / price, price=price), self.PROF, 0.0)

    def test_600_a_007_puntua_tramo_extremo(self) -> None:
        s = self._score(600, 0.07)
        self.assertEqual(s.longshot, W["longshot"])
        self.assertEqual(s.longshot_tier, 0.10)

    def test_600_a_015_no_puntua(self) -> None:
        # tramo 0.20 exige $1.500 (RELEVANCE_TIERS)
        s = self._score(600, 0.15)
        self.assertEqual(s.longshot, 0)
        self.assertIsNone(s.longshot_tier)

    def test_1500_a_015_puntua_tramo_020(self) -> None:
        s = self._score(1500, 0.15)
        self.assertEqual(s.longshot, W["longshot"])
        self.assertEqual(s.longshot_tier, 0.20)

    def test_2600_a_030_puntua_tramo_035_original(self) -> None:
        s = self._score(2600, 0.30)
        self.assertEqual(s.longshot, W["longshot"])
        self.assertEqual(s.longshot_tier, 0.35)

    def test_3000_a_050_fuera_de_tramos(self) -> None:
        s = self._score(3000, 0.50)
        self.assertEqual(s.longshot, 0)
        self.assertIsNone(s.longshot_tier)

    def test_1000_a_030_no_llega_al_minimo(self) -> None:
        # tramo 0.35 exige $1.500 (RELEVANCE_TIERS); $1.000 no llega
        s = self._score(1000, 0.30)
        self.assertEqual(s.longshot, 0)
        self.assertIsNone(s.longshot_tier)

    def test_1800_a_030_puntua_mismo_suelo_que_veto(self) -> None:
        # $1.800 a 0.30 supera el suelo $1.500: longshot puntua (mismo suelo que
        # el veto, no pueden contradecirse).
        s = self._score(1800, 0.30)
        self.assertEqual(s.longshot, W["longshot"])
        self.assertEqual(s.longshot_tier, 0.35)

    def test_8_track_record_desactivado_siempre_cero(self) -> None:
        # Fase 1c: track_record esta DESACTIVADO (peso 0) porque el win_rate no es
        # calculable desde /positions. Ni siquiera un win_rate=0.99 con muestra
        # abundante y marcada como fiable debe puntuar.
        self.assertEqual(W["track_record"], 0)
        prof = FakeProfile(wallet_age_days=300, win_rate=0.99, n_resolved=50, win_rate_reliable=True)
        s = compute_score(_trade(), prof, 0.0)
        self.assertEqual(s.track_record, 0)

    def test_8b_track_record_no_puntua_si_win_rate_no_fiable(self) -> None:
        # Aunque se reactivara el peso, un win_rate no fiable no debe puntuar.
        prof = FakeProfile(wallet_age_days=300, win_rate=0.99, n_resolved=20, win_rate_reliable=False)
        s = compute_score(_trade(), prof, 0.0)
        self.assertEqual(s.track_record, 0)

    def test_9_cluster_y_concentracion(self) -> None:
        prof = FakeProfile(
            wallet_age_days=300, funding_cluster_id="0xFUND",
            concentration=0.8, total_volume_usd=50000,
        )
        s = compute_score(_trade(), prof, 0.0, {"shared_cluster_ids": {"0xFUND"}})
        self.assertEqual(s.cluster, W["cluster"])
        self.assertEqual(s.concentracion, W["concentracion"])
        # funder no compartido -> sin cluster
        s2 = compute_score(_trade(), prof, 0.0, {"shared_cluster_ids": set()})
        self.assertEqual(s2.cluster, 0)

    def test_10_flujo_toxico_y_insensibilidad(self) -> None:
        prof = FakeProfile(wallet_age_days=300)
        # compra Yes (sign +1) con cubo empujando Yes (imbalance +0.9) y volumen
        # suficiente -> flujo toxico
        md = {"same_side_streak": 4, "price_against": True, "bucket_volume_usd": 5000}
        s = compute_score(_trade(side="BUY", outcome="Yes"), prof, 0.9, md)
        self.assertEqual(s.flujo_toxico, W["flujo_toxico"])
        self.assertFalse(s.flujo_toxico_silenciado)
        self.assertEqual(s.insensibilidad_precio, W["insensibilidad_precio"])
        # imbalance fuerte pero en DIRECCION CONTRARIA al trade -> no cuenta
        s2 = compute_score(_trade(side="BUY", outcome="Yes"), prof, -0.9, {"bucket_volume_usd": 5000})
        self.assertEqual(s2.flujo_toxico, 0)
        self.assertFalse(s2.flujo_toxico_silenciado)


class TestFlujoToxicoVolumenMinimo(unittest.TestCase):
    """El flujo toxico solo cuenta si el cubo tiene volumen en $ suficiente."""

    PROF = FakeProfile(wallet_age_days=300)

    def test_volumen_bajo_silencia(self) -> None:
        # imbalance maximo pero cubo de solo $200 -> 0 puntos, silenciado=True
        s = compute_score(_trade(side="BUY", outcome="Yes"), self.PROF, 1.0,
                          {"bucket_volume_usd": 200})
        self.assertEqual(s.flujo_toxico, 0)
        self.assertTrue(s.flujo_toxico_silenciado)

    def test_volumen_suficiente_puntua(self) -> None:
        s = compute_score(_trade(side="BUY", outcome="Yes"), self.PROF, 1.0,
                          {"bucket_volume_usd": 5000})
        self.assertEqual(s.flujo_toxico, W["flujo_toxico"])
        self.assertFalse(s.flujo_toxico_silenciado)

    def test_edge_justo_en_el_umbral(self) -> None:
        # exactamente en el minimo -> puntua (condicion es >=)
        s = compute_score(_trade(side="BUY", outcome="Yes"), self.PROF, 1.0,
                          {"bucket_volume_usd": config.MIN_BUCKET_VOLUME_FOR_TOXICITY_USD})
        self.assertEqual(s.flujo_toxico, W["flujo_toxico"])
        # justo por debajo -> silenciado
        s2 = compute_score(_trade(side="BUY", outcome="Yes"), self.PROF, 1.0,
                          {"bucket_volume_usd": config.MIN_BUCKET_VOLUME_FOR_TOXICITY_USD - 0.01})
        self.assertEqual(s2.flujo_toxico, 0)
        self.assertTrue(s2.flujo_toxico_silenciado)

    def test_sin_direccion_fuerte_no_se_marca_silenciado(self) -> None:
        # imbalance debil: no aplica el componente, no se marca silenciado
        s = compute_score(_trade(side="BUY", outcome="Yes"), self.PROF, 0.1,
                          {"bucket_volume_usd": 200})
        self.assertEqual(s.flujo_toxico, 0)
        self.assertFalse(s.flujo_toxico_silenciado)

    def test_combinado_suma_total(self) -> None:
        # wallet muy fresca + trade grande longshot: fresca(25+10)+tamaño(15)+longshot(20)
        prof = FakeProfile(wallet_age_days=1)
        s = compute_score(_trade(size=200000, price=0.10), prof, 0.0)  # $20000
        esperado = W["wallet_fresca"] + W["wallet_fresca_extra"] + W["tamano_anomalo"] + W["longshot"]
        self.assertEqual(s.score_total, esperado)


class TestSaveAlert(unittest.TestCase):
    def test_guarda_y_devuelve_id_con_json(self) -> None:
        conn = connect(":memory:")
        score = ScoreBreakdown(wallet_fresca=25, tamano_anomalo=15, score_total=40)
        aid = save_alert(
            conn, ts="2026-07-05T00:00:00Z", condition_id="cid",
            market_question="Q?", wallet="0xabc", side="Yes",
            trade_size_usd=12000.0, price_at_detection=0.5, score=score,
            bucket_imbalance=0.8,
        )
        self.assertEqual(aid, 1)
        row = conn.execute(
            "SELECT score_total, score_breakdown, bucket_imbalance FROM alerts WHERE id=?", (aid,)
        ).fetchone()
        self.assertEqual(row[0], 40)
        breakdown = json.loads(row[1])
        self.assertEqual(breakdown["wallet_fresca"], 25)
        self.assertNotIn("score_total", breakdown)  # solo componentes
        self.assertAlmostEqual(row[2], 0.8)
        conn.close()


if __name__ == "__main__":
    unittest.main()
