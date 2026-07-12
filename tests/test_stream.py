"""Tests del detector en tiempo real (parte sincrona, sin red ni asyncio).

Mockean payloads con la forma real del feed de trades de Polymarket para probar
el filtro de politica, el fallback name/pseudonym, el valor en $ = size*price y
el flujo por-trade con SQLite en memoria.
"""
import asyncio
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from src import config
from src.analysis.profiles import WalletProfile
from src.realtime.stream import Detector, display_name, is_politics_trade, send_telegram_alert
from src.realtime import storage
from src.analysis import profiles
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


def _run(coro):
    """Ejecuta una corutina en un event loop efimero (process_trade es async)."""
    return asyncio.run(coro)


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
    def setUp(self) -> None:
        # process_trade envia una notificacion de Telegram al saltar alerta:
        # parcheamos el POST para no tocar la red en los tests.
        patcher = patch("src.realtime.stream.requests.post")
        self.mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        # Con el perfilado bajo demanda, un trade grande dispara build_profile (red).
        # Lo forzamos a fallar => se puntua con perfil None, como antes (tabla vacia).
        bp = patch("src.realtime.stream.build_profile", side_effect=RuntimeError("sin red"))
        self.mock_build = bp.start()
        self.addCleanup(bp.stop)

    def test_descarta_no_politica(self) -> None:
        det = _mem_detector()
        aid = _run(det.process_trade(_trade(conditionId="0xNOPOL")))
        self.assertIsNone(aid)
        self.assertEqual(det.trades_processed, 0)

    def test_trade_politica_genera_alerta_con_valor_en_usd(self) -> None:
        det = _mem_detector()
        # wallet sin perfil + trade $6000 longshot => sin_perfil(20)+longshot(20)=40 <50
        # subimos a $12000 (size 120000 * 0.10) => +tamano_anomalo(15) = 55 >=50
        aid = _run(det.process_trade(_trade(size=120000, price=0.10)))
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
        aid = _run(det.process_trade(_trade(size=100, price=0.50)))
        self.assertIsNone(aid)
        self.assertEqual(det.trades_processed, 1)
        self.assertEqual(det.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)

    @patch.object(config, "TELEGRAM_CHAT_ID", "999")
    def test_alerta_envia_telegram_con_payload(self) -> None:
        det = _mem_detector()
        aid = _run(det.process_trade(_trade(size=120000, price=0.10)))  # score >= 50
        self.assertIsNotNone(aid)
        self.mock_post.assert_called_once()
        args, kwargs = self.mock_post.call_args
        self.assertIn(config.TELEGRAM_BOT_TOKEN, args[0])  # URL con el token
        self.assertTrue(args[0].endswith("/sendMessage"))
        payload = kwargs["json"]
        self.assertEqual(payload["chat_id"], "999")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertIn("<b>Will X happen?</b>", payload["text"])
        self.assertIn("AlphaTrader", payload["text"])
        self.assertIn("$12,000", payload["text"])
        self.assertIn("BUY", payload["text"])

    @patch.object(config, "TELEGRAM_CHAT_ID", "999")
    def test_no_alerta_no_envia_telegram(self) -> None:
        det = _mem_detector()
        _run(det.process_trade(_trade(size=100, price=0.50)))  # score bajo
        self.mock_post.assert_not_called()

    @patch.object(config, "TELEGRAM_CHAT_ID", None)
    def test_sin_chat_id_no_envia(self) -> None:
        det = _mem_detector()
        aid = _run(det.process_trade(_trade(size=120000, price=0.10)))  # score >= 50
        self.assertIsNotNone(aid)          # la alerta se guarda igual
        self.mock_post.assert_not_called()  # pero no se envia sin chat_id

    def test_process_raw_ignora_no_json(self) -> None:
        det = _mem_detector()
        _run(det._process_raw("no soy json (ACK)"))  # no debe lanzar
        _run(det._process_raw(json.dumps(_trade(size=120000, price=0.10))))
        self.assertEqual(det.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)

    def test_process_raw_acepta_lista(self) -> None:
        det = _mem_detector()
        _run(det._process_raw(json.dumps([_trade(size=120000, price=0.10), _trade(conditionId="x")])))
        self.assertEqual(det.trades_processed, 1)  # solo el de politica

    def test_refresh_context_desde_otro_hilo(self) -> None:
        # refresh_context corre en un hilo del executor; no debe usar self.conn
        # (creada en este hilo). Con la conexion propia por hilo, leer los
        # clusters desde otro hilo funciona sin ProgrammingError.
        with tempfile.TemporaryDirectory() as d:
            dbp = os.path.join(d, "t.db")
            pc = profiles.connect(dbp)
            pc.execute("INSERT INTO wallet_profiles (wallet, funding_cluster_id) VALUES ('w1','0xF')")
            pc.execute("INSERT INTO wallet_profiles (wallet, funding_cluster_id) VALUES ('w2','0xF')")
            pc.commit()
            pc.close()

            main_conn = storage.connect(":memory:")  # self.conn creada en ESTE hilo
            det = Detector(main_conn, db_path=dbp)

            with patch("src.realtime.stream.get_politics_events", return_value=[]), \
                 patch("src.realtime.stream.flatten_markets",
                       return_value=[{"condition_id": "0xC"}]):
                t = threading.Thread(target=det.refresh_context)
                t.start()
                t.join()

            # Si se hubiera usado self.conn (otro hilo) => ProgrammingError, capturada
            # dentro de refresh_context, y shared_cluster_ids quedaria vacio.
            self.assertEqual(det.politics_conditions, {"0xC"})
            self.assertEqual(det.shared_cluster_ids, {"0xF"})
            main_conn.close()

    def test_process_raw_desenvuelve_sobre_real(self) -> None:
        # El feed real envuelve el trade en {topic, type, payload, ...}
        det = _mem_detector()
        envelope = {
            "connection_id": "abc==", "topic": "activity", "type": "trades",
            "timestamp": 1783000000, "payload": _trade(size=120000, price=0.10),
        }
        _run(det._process_raw(json.dumps(envelope)))
        self.assertEqual(det.trades_processed, 1)
        self.assertEqual(det.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)


class TestOnDemandProfiling(unittest.TestCase):
    """Perfilado bajo demanda en el detector (Fase 1)."""

    def setUp(self) -> None:
        patcher = patch("src.realtime.stream.requests.post")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _det(self) -> Detector:
        # db_path :memory: => _build_and_save escribe en una BD efimera aparte,
        # sin tocar la BD real del proyecto.
        conn = storage.connect(":memory:")
        conn.executescript(WALLET_PROFILES_SCHEMA)
        conn.executescript(VOLUME_BUCKETS_SCHEMA)
        conn.commit()
        det = Detector(conn, db_path=":memory:")
        det.politics_conditions = {"0xPOL"}
        return det

    def test_trade_pequeno_no_perfila(self) -> None:
        det = self._det()
        with patch("src.realtime.stream.build_profile") as mock_bp:
            _run(det.process_trade(_trade(size=100, price=0.50)))  # $50, sin tramo longshot
        mock_bp.assert_not_called()

    def test_trade_grande_perfila_y_guarda(self) -> None:
        det = self._det()
        prof = WalletProfile(wallet="0xWALLET", wallet_age_days=100.0)
        with patch("src.realtime.stream.build_profile", return_value=prof) as mock_bp, \
             patch("src.realtime.stream.save_profile") as mock_save:
            _run(det.process_trade(_trade(size=30000, price=0.50)))  # $15.000 >= 2500
        mock_bp.assert_called_once_with("0xWALLET")
        mock_save.assert_called_once()
        self.assertEqual(det._profile_cache["0xWALLET"][0], prof)
        self.assertEqual(det._profiled_built, 1)

    def test_caso_maduro_800_a_007_si_perfila(self) -> None:
        det = self._det()
        prof = WalletProfile(wallet="0xWALLET")
        # $800 a 0.07: size = 800/0.07 ~ 11429 shares; < $2500 pero tramo <=0.10.
        with patch("src.realtime.stream.build_profile", return_value=prof) as mock_bp, \
             patch("src.realtime.stream.save_profile"):
            _run(det.process_trade(_trade(size=11429, price=0.07)))
        mock_bp.assert_called_once()

    def test_cache_dentro_de_ttl_no_reperfila(self) -> None:
        det = self._det()
        prof = WalletProfile(wallet="0xWALLET")
        with patch("src.realtime.stream.build_profile", return_value=prof) as mock_bp, \
             patch("src.realtime.stream.save_profile"):
            _run(det.process_trade(_trade(size=30000, price=0.50)))
            _run(det.process_trade(_trade(size=30000, price=0.50)))
        mock_bp.assert_called_once()  # el segundo trade sale de cache
        self.assertEqual(det._profiled_cached, 1)

    def test_perfil_viejo_se_reconstruye(self) -> None:
        det = self._det()
        # Perfil en BD pero caducado (updated_at muy antiguo) => hay que reconstruir.
        det.conn.execute(
            "INSERT INTO wallet_profiles (wallet, updated_at) VALUES ('0xWALLET', '2000-01-01T00:00:00+00:00')"
        )
        det.conn.commit()
        prof = WalletProfile(wallet="0xWALLET")
        with patch("src.realtime.stream.build_profile", return_value=prof) as mock_bp, \
             patch("src.realtime.stream.save_profile"):
            _run(det.process_trade(_trade(size=30000, price=0.50)))
        mock_bp.assert_called_once()

    def test_build_profile_falla_no_rompe_y_puntua_sin_perfil(self) -> None:
        det = self._det()
        with patch("src.realtime.stream.build_profile", side_effect=RuntimeError("timeout")):
            # No debe lanzar; puntua con perfil None.
            _run(det.process_trade(_trade(size=30000, price=0.50)))
        self.assertEqual(det._profiled_failed, 1)
        self.assertIsNone(det._profile_cache["0xWALLET"][0])
        self.assertEqual(det.trades_processed, 1)  # el detector siguio vivo


class TestSendTelegram(unittest.TestCase):
    @patch.object(config, "TELEGRAM_CHAT_ID", "12345")
    @patch("src.realtime.stream.requests.post")
    def test_post_con_texto_formateado(self, mock_post) -> None:
        send_telegram_alert({"market_title": "M", "outcome": "Yes", "score": 70,
                             "username": "u", "size_usd": 12345, "side": "BUY"})
        args, kwargs = mock_post.call_args
        self.assertTrue(args[0].endswith("/sendMessage"))
        self.assertEqual(kwargs["json"]["chat_id"], "12345")
        self.assertIn("<b>M</b>", kwargs["json"]["text"])
        self.assertIn("Score: 70", kwargs["json"]["text"])

    @patch.object(config, "TELEGRAM_CHAT_ID", "12345")
    @patch("src.realtime.stream.requests.post")
    def test_escapa_html_del_titulo(self, mock_post) -> None:
        send_telegram_alert({"market_title": "A<b>&", "outcome": "Yes", "score": 1,
                             "username": "u", "size_usd": 1, "side": "BUY"})
        text = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("A&lt;b&gt;&amp;", text)  # el titulo va escapado

    @patch.object(config, "TELEGRAM_CHAT_ID", None)
    @patch("src.realtime.stream.requests.post")
    def test_sin_chat_id_no_postea(self, mock_post) -> None:
        send_telegram_alert({"market_title": "M", "outcome": "Yes", "score": 1,
                             "username": "u", "size_usd": 1, "side": "BUY"})
        mock_post.assert_not_called()

    @patch.object(config, "TELEGRAM_CHAT_ID", "12345")
    @patch("src.realtime.stream.requests.post",
           side_effect=__import__("requests").RequestException("caida"))
    def test_fallo_de_red_no_rompe(self, _mock_post) -> None:
        # No debe lanzar aunque el POST falle.
        send_telegram_alert({"market_title": "M", "outcome": "Yes", "score": 70,
                             "username": "u", "size_usd": 1, "side": "BUY"})


if __name__ == "__main__":
    unittest.main()
