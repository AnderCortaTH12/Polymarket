import asyncio
import json
from datetime import datetime
from src.analysis.profiles import build_profile  # ya tienes esto
from src.analysis.buckets import bucket_trades
from src.analysis.scoring import compute_score
from src.realtime.storage import save_alert
from src.config import ALERT_THRESHOLD


async def test_websocket():
    import websockets

    uri = "wss://ws-live-data.polymarket.com"

    try:
        async with websockets.connect(uri) as ws:
            # Suscripción correcta
            await ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [
                    {"topic": "activity", "type": "trades", "filters": ""}
                ]
            }))

            # Recibir y imprimir los 10 primeros mensajes crudos
            for i in range(10):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    print(f"\n[Msg {i}] Raw:\n{msg[:200]}")  # primeros 200 chars

                    try:
                        parsed = json.loads(msg)
                        print(f"[Msg {i}] Parsed keys: {list(parsed.keys()) if isinstance(parsed, dict) else 'not a dict'}")
                        print(f"\n[Msg {i}] Payload completo:\n{json.dumps(parsed.get('payload', {}), indent=2)}")
                    except json.JSONDecodeError:
                        print(f"[Msg {i}] No es JSON válido")

                except asyncio.TimeoutError:
                    print(f"\n[Msg {i}] Timeout (5s sin respuesta)")
                    break

    except Exception as e:
        print(f"ERROR: {e}")


if __name__ == "__main__":
    asyncio.run(test_websocket())