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
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src import config, db
from src.analysis.scoring import ScoreBreakdown
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

            with patch("src.realtime.stream.get_political_condition_ids",
                       return_value={"0xC"}):
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


class TestRelevanceVeto(unittest.TestCase):
    """Fase 2: veto de relevancia economica escalonado por precio (portero)."""

    def setUp(self) -> None:
        patcher = patch("src.realtime.stream.requests.post")
        patcher.start()
        self.addCleanup(patcher.stop)
        # Los trades que PASAN el veto disparan perfilado (red); lo forzamos a
        # fallar para no tocar la red (se puntua con perfil None, nos vale).
        bp = patch("src.realtime.stream.build_profile", side_effect=RuntimeError("sin red"))
        bp.start()
        self.addCleanup(bp.stop)

    def _det(self) -> Detector:
        conn = storage.connect(":memory:")
        conn.executescript(WALLET_PROFILES_SCHEMA)
        conn.executescript(VOLUME_BUCKETS_SCHEMA)
        conn.commit()
        det = Detector(conn, db_path=":memory:")
        det.politics_conditions = {"0xPOL"}
        return det

    def test_micro_trade_vetado_aunque_score_potencial_alto(self) -> None:
        # $0.30 a 0.009: size = 0.30/0.009 ~ 33 shares. Wallet muy fresca (score
        # potencial >= 50 por freshness), pero el veto lo descarta antes de puntuar.
        det = self._det()
        prof = WalletProfile(wallet="0xWALLET", wallet_age_days=0.5)
        det.conn.execute(
            "INSERT INTO wallet_profiles (wallet, wallet_age_days, updated_at) "
            "VALUES ('0xWALLET', 0.5, ?)",
            (__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),),
        )
        det.conn.commit()
        with patch("src.realtime.stream.build_profile", return_value=prof) as mock_bp:
            aid = _run(det.process_trade(_trade(size=0.30 / 0.009, price=0.009)))
        self.assertIsNone(aid)
        self.assertEqual(det.vetoed_by_size, 1)
        self.assertEqual(det.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)
        # Veto ANTES del perfilado: no debe gastar red construyendo perfil.
        mock_bp.assert_not_called()

    def test_veto_ocurre_antes_del_perfilado(self) -> None:
        det = self._det()
        with patch("src.realtime.stream.build_profile") as mock_bp:
            _run(det.process_trade(_trade(size=2000 / 0.50, price=0.50)))  # $2.000 @ 0.50, suelo 3.000
        mock_bp.assert_not_called()
        self.assertEqual(det.vetoed_by_size, 1)

    def test_maduro_800_a_007_pasa_el_veto(self) -> None:
        det = self._det()
        aid = _run(det.process_trade(_trade(size=800 / 0.07, price=0.07)))  # suelo 500
        # No vetado (puede o no alertar, pero el veto no lo descarta).
        self.assertEqual(det.vetoed_by_size, 0)
        self.assertEqual(det.trades_processed, 1)
        del aid

    def test_1800_a_030_pasa_el_veto(self) -> None:
        det = self._det()
        _run(det.process_trade(_trade(size=1800 / 0.30, price=0.30)))  # suelo 1.500
        self.assertEqual(det.vetoed_by_size, 0)

    def test_2000_a_050_vetado(self) -> None:
        det = self._det()
        aid = _run(det.process_trade(_trade(size=2000 / 0.50, price=0.50)))  # suelo 3.000
        self.assertIsNone(aid)
        self.assertEqual(det.vetoed_by_size, 1)

    def test_5000_a_050_pasa_el_veto(self) -> None:
        det = self._det()
        _run(det.process_trade(_trade(size=5000 / 0.50, price=0.50)))  # suelo 3.000, $5.000 pasa
        self.assertEqual(det.vetoed_by_size, 0)


class TestPriceExtremeVeto(unittest.TestCase):
    """v6: veto por techo/piso de precio (recorrido maximo a resolucion < 4%)."""

    def setUp(self) -> None:
        patcher = patch("src.realtime.stream.requests.post")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _det(self) -> Detector:
        conn = storage.connect(":memory:")
        conn.executescript(WALLET_PROFILES_SCHEMA)
        conn.executescript(VOLUME_BUCKETS_SCHEMA)
        conn.commit()
        det = Detector(conn, db_path=":memory:")
        det.politics_conditions = {"0xPOL"}
        return det

    def test_precio_098_vetado_por_precio_no_perfila(self) -> None:
        det = self._det()
        # $12.000 a 0.98: tamaño de sobra para pasar el veto de relevancia y
        # perfilar, pero el precio (>= PRICE_CEILING_VETO=0.96) debe vetarlo antes.
        with patch("src.realtime.stream.build_profile") as mock_bp:
            aid = _run(det.process_trade(_trade(size=12000 / 0.98, price=0.98)))
        self.assertIsNone(aid)
        self.assertEqual(det.vetoed_by_price, 1)
        self.assertEqual(det.vetoed_by_size, 0)
        self.assertEqual(det.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)
        mock_bp.assert_not_called()

    def test_precio_002_vetado_por_precio_no_perfila(self) -> None:
        det = self._det()
        # $12.000 a 0.02: idem, pero por el piso (<= PRICE_FLOOR_VETO=0.04).
        with patch("src.realtime.stream.build_profile") as mock_bp:
            aid = _run(det.process_trade(_trade(size=12000 / 0.02, price=0.02)))
        self.assertIsNone(aid)
        self.assertEqual(det.vetoed_by_price, 1)
        self.assertEqual(det.vetoed_by_size, 0)
        self.assertEqual(det.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)
        mock_bp.assert_not_called()

    def test_precio_050_tamano_suficiente_pasa_ambos_vetos_y_puntua(self) -> None:
        det = self._det()
        # $12.000 a 0.50: precio intermedio (pasa el veto de precio), tamaño de
        # sobra (pasa el veto de relevancia, suelo 3.000 a ese precio).
        prof = WalletProfile(wallet="0xWALLET")
        with patch("src.realtime.stream.build_profile", return_value=prof), \
             patch("src.realtime.stream.save_profile"):
            aid = _run(det.process_trade(_trade(size=12000 / 0.50, price=0.50)))
        self.assertEqual(det.vetoed_by_price, 0)
        self.assertEqual(det.vetoed_by_size, 0)
        self.assertEqual(det.trades_processed, 1)
        del aid  # puede o no alertar segun el score; lo relevante es que se puntuo

    def test_caso_maduro_800_a_007_no_vetado_por_precio_genera_alerta(self) -> None:
        det = self._det()
        # $800 a 0.07: precio > PRICE_FLOOR_VETO (0.04) => no vetado por precio.
        # Pasa el veto de tamaño por el tramo longshot (suelo 500 a ese precio).
        prof = WalletProfile(wallet="0xWALLET", wallet_age_days=0.5)
        with patch("src.realtime.stream.build_profile", return_value=prof), \
             patch("src.realtime.stream.save_profile"):
            aid = _run(det.process_trade(_trade(size=800 / 0.07, price=0.07)))
        self.assertEqual(det.vetoed_by_price, 0)
        self.assertEqual(det.vetoed_by_size, 0)
        self.assertIsNotNone(aid)  # sigue generando alerta como antes de v6
        row = det.conn.execute(
            "SELECT scoring_version FROM alerts WHERE id=?", (aid,)
        ).fetchone()
        self.assertEqual(row[0], config.SCORING_VERSION)
        self.assertEqual(row[0], "v6")


class TestNotifyDedupe(unittest.TestCase):
    """Anti-spam: deduplicar notificaciones de Telegram por (wallet, mercado)."""

    def setUp(self) -> None:
        # Espiamos el envio de Telegram sin tocar la red.
        self.send = patch("src.realtime.stream.send_telegram_alert").start()
        self.addCleanup(patch.stopall)
        # Perfilado sin red (perfil None): no afecta al alerta ni al dedupe.
        patch("src.realtime.stream.build_profile", side_effect=RuntimeError("sin red")).start()

    def _det(self) -> Detector:
        conn = storage.connect(":memory:")
        conn.executescript(WALLET_PROFILES_SCHEMA)
        conn.executescript(VOLUME_BUCKETS_SCHEMA)
        conn.commit()
        det = Detector(conn, db_path=":memory:")
        det.politics_conditions = {"0xPOL", "0xPOL2"}
        return det

    def _alert_trade(self, **over):
        # Trade que supera el umbral: $12.000 a 0.10 (sin_perfil+tamano+longshot).
        base = dict(size=120000, price=0.10)
        base.update(over)
        return _trade(**base)

    def _notified_at(self, det, alert_id):
        return det.conn.execute(
            "SELECT notified_at FROM alerts WHERE id=?", (alert_id,)
        ).fetchone()[0]

    def test_primera_alerta_notifica_y_marca_notified_at(self) -> None:
        det = self._det()
        aid = _run(det.process_trade(self._alert_trade()))
        self.assertIsNotNone(aid)
        self.send.assert_called_once()
        self.assertEqual(det.notifs_sent, 1)
        self.assertEqual(det.notifs_deduped, 0)
        self.assertIsNotNone(self._notified_at(det, aid))

    def test_segunda_misma_wallet_mercado_dentro_de_ventana_no_notifica(self) -> None:
        det = self._det()
        aid1 = _run(det.process_trade(self._alert_trade()))
        aid2 = _run(det.process_trade(self._alert_trade()))
        # Solo se envio una vez; la alerta 2 SI se guardo pero con notified_at NULL.
        self.send.assert_called_once()
        self.assertEqual(det.notifs_sent, 1)
        self.assertEqual(det.notifs_deduped, 1)
        self.assertIsNotNone(self._notified_at(det, aid1))
        self.assertIsNone(self._notified_at(det, aid2))
        self.assertEqual(det.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 2)

    def test_misma_wallet_otro_mercado_si_notifica(self) -> None:
        det = self._det()
        _run(det.process_trade(self._alert_trade(conditionId="0xPOL")))
        _run(det.process_trade(self._alert_trade(conditionId="0xPOL2")))
        self.assertEqual(self.send.call_count, 2)  # clave (wallet, mercado) distinta
        self.assertEqual(det.notifs_deduped, 0)

    def test_pasada_la_ventana_vuelve_a_notificar(self) -> None:
        det = self._det()
        aid1 = _run(det.process_trade(self._alert_trade()))
        # Envejecemos la primera notificacion 7h (> NOTIFY_DEDUPE_HOURS).
        old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        det.conn.execute("UPDATE alerts SET notified_at=? WHERE id=?", (old, aid1))
        det.conn.commit()
        _run(det.process_trade(self._alert_trade()))
        self.assertEqual(self.send.call_count, 2)  # fuera de ventana -> reenvia
        self.assertEqual(det.notifs_deduped, 0)

    def test_alerta_siempre_se_guarda_aunque_se_dedupee(self) -> None:
        det = self._det()
        for _ in range(3):
            _run(det.process_trade(self._alert_trade()))
        # 3 alertas guardadas, 1 notificacion enviada, 2 deduplicadas.
        self.assertEqual(det.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 3)
        self.assertEqual(det.notifs_sent, 1)
        self.assertEqual(det.notifs_deduped, 2)


class TestStorageDedupeHelpers(unittest.TestCase):
    """Helpers de dedupe y migracion idempotente de notified_at."""

    def test_recent_notification_exists_y_mark(self) -> None:
        conn = storage.connect(":memory:")
        score = ScoreBreakdown(score_total=60)
        aid = storage.save_alert(
            conn, ts="2026-07-16T00:00:00+00:00", condition_id="0xC", market_question="M",
            wallet="0xW", side="Yes", trade_size_usd=12000.0, price_at_detection=0.1, score=score,
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        # Sin notificar todavia -> no existe.
        self.assertFalse(storage.recent_notification_exists(conn, "0xW", "0xC", cutoff))
        storage.mark_notified(conn, aid, datetime.now(timezone.utc).isoformat())
        self.assertTrue(storage.recent_notification_exists(conn, "0xW", "0xC", cutoff))
        # Otra clave (mercado distinto) no cuenta.
        self.assertFalse(storage.recent_notification_exists(conn, "0xW", "0xOTRO", cutoff))
        conn.close()

    def test_migracion_notified_at_idempotente(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            # Tabla vieja SIN notified_at.
            conn = db.connect(path)
            conn.executescript(
                "CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, "
                "condition_id TEXT, wallet TEXT, score_total INTEGER);"
            )
            conn.execute("INSERT INTO alerts (ts, score_total) VALUES ('2026-01-01T00:00:00+00:00', 60)")
            conn.commit()
            conn.close()

            conn = storage.connect(path)  # migra
            cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}
            self.assertIn("notified_at", cols)
            val = conn.execute("SELECT notified_at FROM alerts").fetchone()[0]
            self.assertIsNone(val)  # las viejas quedan sin notificar
            conn.close()

            conn = storage.connect(path)  # reejecutar no falla
            cols2 = {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}
            self.assertIn("notified_at", cols2)
            conn.close()


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


class TestLoggingConfiguration(unittest.TestCase):
    """Verifica que la configuración de logging usa RotatingFileHandler."""

    def test_configure_logging_usa_rotating_handler(self) -> None:
        import logging.handlers
        from pathlib import Path

        log_path = Path(__file__).parent / ".." / "logs" / "test_detector.log"
        log_dir = log_path.parent

        try:
            with patch("src.realtime.stream.LOG_FILE", log_path), \
                 patch("src.realtime.stream.LOG_DIR", log_dir):
                from src.realtime.stream import configure_logging

                # Limpiar handlers existentes para este test.
                root_logger = logging.getLogger()
                old_handlers = root_logger.handlers[:]
                for h in old_handlers:
                    root_logger.removeHandler(h)
                    if hasattr(h, 'close'):
                        h.close()

                try:
                    configure_logging()

                    root_logger = logging.getLogger()
                    rotating_handlers = [
                        h for h in root_logger.handlers
                        if isinstance(h, logging.handlers.RotatingFileHandler)
                    ]
                    self.assertGreater(len(rotating_handlers), 0,
                                      "No RotatingFileHandler found in logging configuration")

                    handler = rotating_handlers[0]
                    self.assertEqual(handler.maxBytes, 20 * 1024 * 1024, "maxBytes debe ser 20 MB")
                    self.assertEqual(handler.backupCount, 5, "backupCount debe ser 5")
                finally:
                    # Restaurar handlers originales y cerrar los nuevos.
                    for h in root_logger.handlers[:]:
                        root_logger.removeHandler(h)
                        if hasattr(h, 'close'):
                            h.close()
                    for h in old_handlers:
                        root_logger.addHandler(h)
        finally:
            # Limpiar archivo de prueba si existe.
            if log_path.exists():
                try:
                    log_path.unlink()
                except PermissionError:
                    pass


if __name__ == "__main__":
    unittest.main()
