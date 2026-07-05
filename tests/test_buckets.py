"""Tests de los cubos de volumen (VPIN) — funciones puras, sin red."""
import unittest

from src.analysis.buckets import bucket_trades, signed_imbalance


def _t(side: str, outcome: str, size: float, price: float, ts: int) -> dict:
    return {"side": side, "outcome": outcome, "size": size, "price": price, "timestamp": ts}


class TestSignedImbalance(unittest.TestCase):
    def test_todo_compra_yes_es_uno(self) -> None:
        bucket = [_t("BUY", "Yes", 100, 0.5, 1), _t("BUY", "Yes", 200, 0.5, 2)]
        self.assertAlmostEqual(signed_imbalance(bucket), 1.0)

    def test_50_50_compra_venta_es_cero(self) -> None:
        # misma Yes: una compra y una venta del mismo tamaño -> 0
        bucket = [_t("BUY", "Yes", 100, 0.5, 1), _t("SELL", "Yes", 100, 0.5, 2)]
        self.assertAlmostEqual(signed_imbalance(bucket), 0.0)

    def test_comprar_no_empuja_negativo(self) -> None:
        # comprar No equivale a apostar contra Yes -> signo negativo
        bucket = [_t("BUY", "No", 100, 0.5, 1)]
        self.assertAlmostEqual(signed_imbalance(bucket), -1.0)
        # vender No empuja Yes -> positivo
        self.assertAlmostEqual(signed_imbalance([_t("SELL", "No", 100, 0.5, 1)]), 1.0)

    def test_bucket_vacio_es_cero(self) -> None:
        self.assertEqual(signed_imbalance([]), 0.0)

    def test_ponderado_por_volumen(self) -> None:
        # compra grande Yes ($900) vs venta pequeña Yes ($100) -> +0.8
        bucket = [_t("BUY", "Yes", 900, 1.0, 1), _t("SELL", "Yes", 100, 1.0, 2)]
        self.assertAlmostEqual(signed_imbalance(bucket), 0.8)


class TestBucketTrades(unittest.TestCase):
    def test_orden_cronologico_y_cierre(self) -> None:
        # 10 trades de $2000 c/u, cubos de $5000 -> se cierran a los 3 trades
        # (3*2000=6000>=5000). 10 trades -> cubos de 3,3,3 y 1 (parcial) = 4 cubos.
        trades = [_t("BUY", "Yes", 2000, 1.0, ts) for ts in range(10, 0, -1)]  # desordenados
        buckets = bucket_trades("cid", trades, bucket_size_usd=5000)
        self.assertEqual(len(buckets), 4)
        # secuencia correlativa
        self.assertEqual([b["bucket_seq"] for b in buckets], [0, 1, 2, 3])
        # ordenados cronologicamente: cada ts_open <= ts_close y crecientes
        self.assertEqual(buckets[0]["ts_open"], 1)
        self.assertTrue(all(b["ts_open"] <= b["ts_close"] for b in buckets))
        opens = [b["ts_open"] for b in buckets]
        self.assertEqual(opens, sorted(opens))
        # los 3 primeros cubos superan el tamaño, el ultimo es parcial
        self.assertGreaterEqual(buckets[0]["volume_usd"], 5000)
        self.assertEqual(buckets[3]["volume_usd"], 2000)

    def test_un_solo_trade(self) -> None:
        buckets = bucket_trades("cid", [_t("BUY", "Yes", 100, 0.5, 5)], bucket_size_usd=5000)
        self.assertEqual(len(buckets), 1)
        b = buckets[0]
        self.assertEqual(b["ts_open"], b["ts_close"])
        self.assertAlmostEqual(b["signed_imbalance"], 1.0)
        self.assertAlmostEqual(b["price_open"], 0.5)

    def test_sin_trades(self) -> None:
        self.assertEqual(bucket_trades("cid", [], bucket_size_usd=5000), [])

    def test_precio_open_close_en_espacio_yes(self) -> None:
        # trade de No a 0.30 -> precio Yes = 0.70
        buckets = bucket_trades("cid", [_t("BUY", "No", 100, 0.30, 1)], bucket_size_usd=5000)
        self.assertAlmostEqual(buckets[0]["price_open"], 0.70)


if __name__ == "__main__":
    unittest.main()
