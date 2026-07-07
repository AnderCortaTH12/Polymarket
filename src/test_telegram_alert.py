"""Script de prueba: envia un mensaje de ejemplo a Telegram.

Sirve para validar que las notificaciones funcionan antes de esperar a una
alerta real del detector. Ejecutar desde la raiz del proyecto:

    cd ~/Polymarket
    source .venv/bin/activate
    python src/test_telegram_alert.py

Si esta bien configurado (TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en src/config.py,
ver DESPLIEGUE_VPS.md), recibiras un mensaje en Telegram al instante.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Permite ejecutar el script directamente (python src/test_telegram_alert.py):
# añade la raiz del proyecto al sys.path para poder importar el paquete `src`.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from src.realtime.stream import send_telegram_alert


def main() -> None:
    """Envia un mensaje de prueba y confirma por pantalla."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:  # que los simbolos no rompan en consolas no-UTF8 (Windows cp1252)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    if not TELEGRAM_BOT_TOKEN:
        print("✗ Falta TELEGRAM_BOT_TOKEN en src/config.py")
        return
    if not TELEGRAM_CHAT_ID:
        print("✗ Falta TELEGRAM_CHAT_ID en src/config.py. Escribe /start al bot y obtenlo con:\n"
              "   python -c \"from src.realtime.stream import get_telegram_chat_id; print(get_telegram_chat_id())\"")
        return

    alert_info = {
        "market_title": "[TEST] Bitcoin Price Movement",
        "outcome": "Up",
        "size_usd": 5000,
        "username": "test-user",
        "side": "BUY",
        "score": 65,
    }
    send_telegram_alert(alert_info)
    print("✓ Mensaje de prueba enviado a Telegram")


if __name__ == "__main__":
    main()
