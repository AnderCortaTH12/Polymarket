"""Tests del detector en tiempo real (parte sincrona, sin red ni asyncio).

Mockean payloads con la forma real del feed de trades de Polymarket para probar
el filtro de politica, el fallback name/pseudonym, el valor en $ = size*price y
el flujo por-trade con SQLite en memoria.
"""
import json
import unittest

from src.realtime.stream import Detector, display_name, is_politics_trade
from src.realtime import storage
from src.analysis.profiles import WALLET_PROFILES_SCHEMA
from src.analysis.buckets import VOLUME_BUCKETS_SCHEMA


def _trade(**over) -> dict:
    """Payload de trade con la forma real del feed (campos que usamos)."""
    base = {
        "conditionId": "0xPOL",
        "proxyWallet": "0xWALLET",
        "name": "AlphaTrader",
        "pseudonym": "Brave-Otter",
        "outcome": "Yes",
        "outcomeIndex": 0,
        "side": "BUY",
        "price": 0.10,
        "size": 60000,          # $6000 = 60000 * 0.10
        "timestamp": 1783000000,
        "title": "Will X happen?",
        "slug": "will-x-happen",
        "transactionHash": "0xdeadbeef",
    }
    base.update(over)
    return base


def _mem_detector() -> Detector:
    conn = storage.connect(":memory:")
    conn.executescript(WALLET_PROFILES_SCHEMA)
    conn.executescript(VOLUME_BUCKETS_SCHEMA)
    conn.commit()
    det = Detector(conn)
    det.politics_conditions = {"0xPOL"}
    return det


class TestPureHelpers(unittest.TestCase):
    def test_display_name_usa_name(self) -> None:
        self.assertEqual(display_name(_trade()), "AlphaTrader")

    def test_display_name_fallback_pseudonym(self) -> None:
        self.assertEqual(display_name(_trade(name="")), "Brave-Otter")
        self.assertEqual(display_name(_trade(name="   ")), "Brave-Otter")

    def test_filtro_politica(self) -> None:
        conds = {"0xPOL"}
        self.assertTrue(is_politics_trade(_trade(conditionId="0xPOL"), conds))
        self.assertFalse(is_politics_trade(_trade(conditionId="0xOTRO"), conds))


class TestProcessTrade(unittest.TestCase):
    def test_descarta_no_politica(self) -> None:
        det = _mem_detector()
        aid = det.process_trade(_trade(conditionId="0xNOPOL"))
        self.assertIsNone(aid)
        self.assertEqual(det.trades_processed, 0)

    def test_trade_politica_genera_alerta_con_valor_en_usd(self) -> None:
        det = _mem_detector()
        # wallet sin perfil + trade $6000 longshot => sin_perfil(20)+longshot(20)=40 <50
        # subimos a $12000 (size 120000 * 0.10) => +tamano_anomalo(15) = 55 >=50
        aid = det.process_trade(_trade(size=120000, price=0.10))
        self.assertIsNotNone(aid)
        row = det.conn.execute(
            "SELECT trade_size_usd, username, transaction_hash, side, score_total "
            "FROM alerts WHERE id=?", (aid,)
        ).fetchone()
        self.assertAlmostEqual(row[0], 12000.0)   # valor en $ = size*price
        self.assertEqual(row[1], "AlphaTrader")   # username guardado
        self.assertEqual(row[2], "0xdeadbeef")    # tx hash guardado
        self.assertEqual(row[3], "Yes")
        self.assertGreaterEqual(row[4], 50)
        self.assertEqual(det.trades_processed, 1)

    def test_trade_pequeno_no_alerta(self) -> None:
        det = _mem_detector()
        # trade pequeño, wallet sin perfil => score bajo, sin alerta pero sí procesado
        aid = det.process_trade(_trade(size=100, price=0.50))
        self.assertIsNone(aid)
        self.assertEqual(det.trades_processed, 1)
        self.assertEqual(det.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)

    def test_process_raw_ignora_no_json(self) -> None:
        det = _mem_detector()
        det._process_raw("no soy json (ACK)")  # no debe lanzar
        det._process_raw(json.dumps(_trade(size=120000, price=0.10)))
        self.assertEqual(det.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)

    def test_process_raw_acepta_lista(self) -> None:
        det = _mem_detector()
        det._process_raw(json.dumps([_trade(size=120000, price=0.10), _trade(conditionId="x")]))
        self.assertEqual(det.trades_processed, 1)  # solo el de politica

    def test_process_raw_desenvuelve_sobre_real(self) -> None:
        # El feed real envuelve el trade en {topic, type, payload, ...}
        det = _mem_detector()
        envelope = {
            "connection_id": "abc==", "topic": "activity", "type": "trades",
            "timestamp": 1783000000, "payload": _trade(size=120000, price=0.10),
        }
        det._process_raw(json.dumps(envelope))
        self.assertEqual(det.trades_processed, 1)
        self.assertEqual(det.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
