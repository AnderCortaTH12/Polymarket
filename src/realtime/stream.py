"""Paso 3b de la Fase 6: servicio de deteccion en tiempo real (WebSocket).

Se conecta al feed de trades en vivo de Polymarket, filtra los de politica,
puntua cada trade con `compute_score` usando perfiles precomputados (SQLite) y
el estado del cubo de volumen en memoria, y guarda una alerta si el score supera
el umbral. Un solo proceso asyncio: sin threads complejos.

Robustez: reconexion con backoff, PING cada 5s, watchdog de inactividad (recv
con timeout), heartbeat por minuto en `service_health` y logging a fichero.

Detectamos ANOMALIAS compatibles con trading informado, no "insiders": el
backtest dira que combinaciones de score tienen valor.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import websockets

from src import config
from src.analysis.buckets import (
    DEFAULT_BUCKET_SIZE_USD,
    _trade_sign,
    _trade_usd,
    _yes_price,
    save_buckets,
    signed_imbalance,
)
from src import db
from src.analysis.profiles import load_profile, load_shared_cluster_ids
from src.analysis.scoring import compute_score
from src.client.gamma import flatten_markets, get_politics_events
from src.collector.models import DB_PATH
from src.realtime import storage

logger = logging.getLogger(__name__)

# No hardcodear URLs sueltas: constantes al inicio del modulo.
WS_URL: str = "wss://ws-live-data.polymarket.com"
NTFY_BASE_URL: str = "https://ntfy.sh"  # notificaciones push (canal en config)
NTFY_TIMEOUT_S: int = 10
SUBSCRIBE_MSG: dict[str, Any] = {
    "action": "subscribe",
    "subscriptions": [{"topic": "activity", "type": "trades", "filters": ""}],
}

PING_INTERVAL_S: float = 5.0
INACTIVITY_TIMEOUT_S: float = 120.0     # sin mensajes en 2 min => reconectar
HEARTBEAT_INTERVAL_S: float = 60.0
REFRESH_INTERVAL_S: float = 600.0       # refrescar set de politica y clusters cada 10 min
RECONNECT_BACKOFF_MAX_S: float = 30.0

LOG_DIR: Path = Path(__file__).resolve().parents[2] / "logs"
LOG_FILE: Path = LOG_DIR / "detector.log"


# --------------------------------------------------------------------------- #
# Helpers puros (testeables sin red)
# --------------------------------------------------------------------------- #
def display_name(trade: dict[str, Any]) -> str:
    """Username a mostrar: `name`, con fallback a `pseudonym` si viene vacio."""
    name = (trade.get("name") or "").strip()
    if name:
        return name
    return (trade.get("pseudonym") or "").strip()


def is_politics_trade(trade: dict[str, Any], politics_conditions: set[str]) -> bool:
    """True si el conditionId del trade esta en el set de mercados de politica."""
    return trade.get("conditionId") in politics_conditions


def send_ntfy_alert(alert_info: dict[str, Any]) -> None:
    """Envia una notificacion push a ntfy.sh por una alerta. Nunca lanza.

    Es un extra: la alerta ya se guardo en BD. Si el POST falla (red caida,
    etc.) se loguea WARNING y el flujo del detector continua sin romperse.
    """
    title = f"{alert_info.get('market_title', '')} - {alert_info.get('outcome', '')}"
    message = (
        f"Score: {alert_info.get('score')} | {alert_info.get('username', '')} | "
        f"${alert_info.get('size_usd', 0):,.0f} | {alert_info.get('side', '')}"
    )
    try:
        requests.post(
            f"{NTFY_BASE_URL}/{config.NTFY_CHANNEL}",
            json={"title": title, "message": message},
            timeout=NTFY_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        logger.warning("No se pudo enviar la notificacion ntfy: %s", exc)


def _to_iso(unix_ts: Any) -> str:
    """Convierte un timestamp unix (segundos) a ISO UTC."""
    try:
        return datetime.fromtimestamp(int(unix_ts), timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Estado de cubos de volumen en memoria (por mercado)
# --------------------------------------------------------------------------- #
class _BucketState:
    """Cubo de volumen en curso de un mercado; se cierra al alcanzar el tamaño."""

    def __init__(self, bucket_size_usd: float) -> None:
        self.bucket_size_usd = bucket_size_usd
        self.trades: list[dict[str, Any]] = []
        self.volume_usd = 0.0
        self.seq = 0

    def add(self, trade: dict[str, Any]) -> tuple[float, float, dict[str, Any] | None]:
        """Añade un trade. Devuelve (imbalance, volumen_del_cubo, cubo_cerrado|None).

        El imbalance y el volumen incluyen este trade (es el "cubo actual"). Si
        con este trade el cubo alcanza el tamaño objetivo, se cierra y se devuelve
        su dict para persistir, y el estado se reinicia para el siguiente cubo.
        """
        self.trades.append(trade)
        self.volume_usd += _trade_usd(trade)
        imbalance = signed_imbalance(self.trades)
        volume_at_trade = self.volume_usd  # volumen incluido este trade (antes de reset)

        closed: dict[str, Any] | None = None
        if self.volume_usd >= self.bucket_size_usd:
            prices = [p for p in (_yes_price(t) for t in self.trades) if p is not None]
            closed = {
                "bucket_seq": self.seq,
                "ts_open": int(self.trades[0].get("timestamp", 0)),
                "ts_close": int(self.trades[-1].get("timestamp", 0)),
                "volume_usd": self.volume_usd,
                "signed_imbalance": imbalance,
                "price_open": prices[0] if prices else None,
                "price_close": prices[-1] if prices else None,
            }
            self.seq += 1
            self.trades = []
            self.volume_usd = 0.0
        return imbalance, volume_at_trade, closed


# --------------------------------------------------------------------------- #
# Detector
# --------------------------------------------------------------------------- #
class Detector:
    """Procesa trades entrantes: filtra politica, puntua y genera alertas.

    La logica por-trade (`process_trade`) es sincrona y testeable; el bucle
    asyncio (`run`) solo se encarga de la conexion, PING, watchdog y refrescos.
    """

    def __init__(
        self,
        conn: Any,
        bucket_size_usd: float = DEFAULT_BUCKET_SIZE_USD,
        db_path: Any = DB_PATH,
    ) -> None:
        # self.conn es la conexion del camino caliente (process_trade, heartbeat),
        # usada SIEMPRE desde el hilo del event loop. db_path se usa para abrir
        # conexiones nuevas en otros hilos (refresh_context corre en un executor).
        self.conn = conn
        self.db_path = db_path
        self.bucket_size_usd = bucket_size_usd
        self.politics_conditions: set[str] = set()
        self.shared_cluster_ids: set[str] = set()
        self._buckets: dict[str, _BucketState] = {}
        self._streaks: dict[tuple[str, str], dict[str, Any]] = {}
        self.trades_processed = 0
        self.ws_connected = False

    # --- estado por-wallet para "insensibilidad al precio" -------------------
    def _update_streak(self, wallet: str, cid: str, trade: dict[str, Any]) -> tuple[int, bool]:
        """Actualiza la racha de mismo-lado de una wallet y si paga peor precio.

        Devuelve (racha, price_against) donde price_against es True si sigue
        acumulando el mismo lado a un precio de entrada peor (mas caro) que al
        empezar la racha.
        """
        key = (wallet, cid)
        sign = _trade_sign(trade)
        try:
            price = float(trade.get("price"))
        except (TypeError, ValueError):
            price = None
        st = self._streaks.get(key)
        if st and st["sign"] == sign and sign != 0:
            st["streak"] += 1
        else:
            st = {"sign": sign, "streak": 1, "start_price": price}
            self._streaks[key] = st
        price_against = (
            price is not None and st["start_price"] is not None and price > st["start_price"]
        )
        return st["streak"], price_against

    def process_trade(self, trade: dict[str, Any]) -> int | None:
        """Flujo por trade (pasos 2-7 del diseño). Devuelve alert_id o None.

        No hace llamadas de red: perfil desde SQLite (o None si no existe) y
        estado de cubos en memoria.
        """
        # 2. Filtro de politica
        if not is_politics_trade(trade, self.politics_conditions):
            return None
        self.trades_processed += 1

        wallet = trade.get("proxyWallet") or ""
        cid = trade.get("conditionId") or ""

        # 3. Perfil (lectura local; None si no visto => scoring conservador)
        profile = load_profile(self.conn, wallet) if wallet else None

        # 4. Cubo de volumen del mercado (crear si no existe)
        bucket = self._buckets.setdefault(cid, _BucketState(self.bucket_size_usd))
        imbalance, bucket_volume, closed = bucket.add(trade)
        if closed is not None:
            save_buckets(self.conn, cid, [closed])

        # contexto para el scorer
        streak, price_against = self._update_streak(wallet, cid, trade)
        market_data = {
            "shared_cluster_ids": self.shared_cluster_ids,
            "same_side_streak": streak,
            "price_against": price_against,
            "bucket_volume_usd": bucket_volume,
        }

        # 5. Score (con valor en $ real = size * price dentro de compute_score)
        score = compute_score(trade, profile, imbalance, market_data)

        # 6. Alerta si supera umbral
        if score.score_total < config.ALERT_THRESHOLD:
            return None

        try:
            price = float(trade.get("price"))
        except (TypeError, ValueError):
            price = None
        alert_id = storage.save_alert(
            self.conn,
            ts=_to_iso(trade.get("timestamp")),
            condition_id=cid,
            market_question=trade.get("title") or trade.get("slug") or "",
            wallet=wallet,
            side=trade.get("outcome") or trade.get("side") or "",
            trade_size_usd=_trade_usd(trade),
            price_at_detection=price,
            score=score,
            bucket_imbalance=imbalance,
            username=display_name(trade),
            transaction_hash=trade.get("transactionHash"),
            market_slug=trade.get("slug"),
            trade_side=trade.get("side"),
        )
        logger.info(
            "ALERTA score=%d %s por %s (%s) $%.0f tx=%s",
            score.score_total, cid, display_name(trade), wallet,
            _trade_usd(trade), trade.get("transactionHash"),
        )

        # 7b. Notificacion push (bonus; nunca debe romper el flujo del detector)
        if alert_id:
            send_ntfy_alert({
                "market_title": trade.get("title") or trade.get("slug") or "",
                "outcome": trade.get("outcome") or "",
                "score": score.score_total,
                "username": display_name(trade),
                "size_usd": _trade_usd(trade),
                "side": trade.get("side") or "",
            })
        return alert_id

    # --- refrescos (con red, fuera del camino critico) ----------------------
    def refresh_context(self) -> None:
        """Recarga el set de condition_ids de politica (Gamma) y los clusters (SQLite).

        Corre en un hilo del executor (via asyncio.to_thread), por lo que NO usa
        self.conn (creada en el hilo del event loop; SQLite prohibe compartir una
        conexion entre hilos). Abre su propia conexion efimera en este hilo para
        la lectura; WAL permite que coexista con las escrituras del camino caliente.
        """
        try:
            markets = flatten_markets(get_politics_events())
            self.politics_conditions = {m["condition_id"] for m in markets if m.get("condition_id")}
            conn = db.connect(self.db_path)
            try:
                self.shared_cluster_ids = load_shared_cluster_ids(conn)
            finally:
                conn.close()
            logger.info(
                "Contexto refrescado: %d mercados de politica, %d clusters compartidos",
                len(self.politics_conditions), len(self.shared_cluster_ids),
            )
        except Exception:  # noqa: BLE001 - un fallo de refresco no debe tumbar el servicio
            logger.exception("Fallo refrescando contexto (se reintenta en el proximo ciclo)")

    def _process_raw(self, raw: str) -> None:
        """Parsea un mensaje del WebSocket. Ignora lo que no sea JSON (ACK/PONG).

        Cada mensaje del feed es un sobre `{topic, type, payload, ...}` donde el
        trade real esta en `payload`; se desenvuelve antes de procesar. Se acepta
        tambien un trade "pelado" (sin sobre) por robustez.
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return  # primer ACK o PONG: no es error
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            trade = item.get("payload", item)  # desenvolver el sobre
            for t in (trade if isinstance(trade, list) else [trade]):
                if isinstance(t, dict):
                    try:
                        self.process_trade(t)
                    except Exception:  # noqa: BLE001 - un trade malformado no para el stream
                        logger.exception("Fallo procesando un trade")

    # --- corutinas del servicio ---------------------------------------------
    async def _ping_loop(self, ws: Any) -> None:
        """Envia PING cada 5s; sin esto el servidor cierra la conexion."""
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            await ws.send("PING")

    async def _heartbeat_loop(self) -> None:
        """Escribe un latido por minuto en service_health."""
        while True:
            storage.save_heartbeat(
                self.conn, datetime.now(timezone.utc).isoformat(),
                self.trades_processed, self.ws_connected,
            )
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    async def _refresh_loop(self) -> None:
        """Refresca el contexto cada 10 min (en un hilo para no bloquear el loop)."""
        while True:
            await asyncio.sleep(REFRESH_INTERVAL_S)
            await asyncio.to_thread(self.refresh_context)

    async def run(self) -> None:
        """Bucle principal: conecta, se suscribe y procesa, reconectando con backoff."""
        await asyncio.to_thread(self.refresh_context)  # carga inicial
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        refresh = asyncio.create_task(self._refresh_loop())
        backoff = 1.0
        try:
            while True:
                ping_task: asyncio.Task | None = None
                try:
                    async with websockets.connect(WS_URL, ping_interval=None) as ws:
                        await ws.send(json.dumps(SUBSCRIBE_MSG))
                        self.ws_connected = True
                        backoff = 1.0
                        ping_task = asyncio.create_task(self._ping_loop(ws))
                        logger.info("Conectado y suscrito a %s", WS_URL)
                        while True:
                            raw = await asyncio.wait_for(ws.recv(), timeout=INACTIVITY_TIMEOUT_S)
                            self._process_raw(raw)
                except Exception as exc:  # noqa: BLE001 - reconectar ante cualquier fallo
                    logger.warning("WebSocket caido (%s). Reconectando en %.0fs", exc, backoff)
                finally:
                    self.ws_connected = False
                    if ping_task is not None:
                        ping_task.cancel()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_S)
        finally:
            heartbeat.cancel()
            refresh.cancel()


def configure_logging() -> None:
    """Logging a consola y a fichero logs/detector.log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )


def main() -> None:
    """Arranca el detector."""
    configure_logging()
    conn = storage.connect()
    # aseguramos tambien las tablas de perfiles y cubos (mismo SQLite)
    from src.analysis.buckets import VOLUME_BUCKETS_SCHEMA
    from src.analysis.profiles import WALLET_PROFILES_SCHEMA
    conn.executescript(WALLET_PROFILES_SCHEMA)
    conn.executescript(VOLUME_BUCKETS_SCHEMA)
    conn.commit()
    detector = Detector(conn)
    try:
        asyncio.run(detector.run())
    except KeyboardInterrupt:
        logger.info("Detector detenido por el usuario")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
