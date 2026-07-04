"""Cliente de la Polygonscan API para el cruce on-chain de wallets.

Usado para identificar quien financio originalmente una wallet proxy de
Polymarket: la primera transaccion ENTRANTE en Polygon nos da el remitente
("funding source"). La API key se lee de la variable de entorno
POLYGONSCAN_API_KEY (o de un fichero .env en la raiz del proyecto).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from src.client.http import get_json

logger = logging.getLogger(__name__)

# No hardcodear URLs sueltas: constantes al inicio del modulo.
# Polygonscan migro a la API V2 unificada de Etherscan: el antiguo
# api.polygonscan.com/api esta deprecado (devuelve HTML). Se usa el endpoint
# multichain de Etherscan con chainid=137 (Polygon).
POLYGONSCAN_BASE_URL: str = "https://api.etherscan.io/v2/api"
POLYGON_CHAIN_ID: int = 137
POLYGONSCAN_API_KEY_ENV: str = "POLYGONSCAN_API_KEY"

# Contratos de USDC en Polygon. Las proxy wallets de Polymarket se financian
# con USDC.e (USDC puenteado), que es el que aparece de verdad en las wallets
# de prueba; por eso es el contrato por defecto. Se deja tambien el USDC nativo
# como referencia por si en el futuro cambian.
USDC_E_CONTRACT_POLYGON: str = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"  # USDC.e (puenteado)
USDC_NATIVE_CONTRACT_POLYGON: str = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"  # USDC nativo
USDC_CONTRACT_POLYGON: str = USDC_E_CONTRACT_POLYGON

# Rate limit del plan gratuito: 5 req/segundo. Dejamos un pequeno margen.
MIN_INTERVAL_SECONDS: float = 0.22

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _PROJECT_ROOT / ".env"

# Estado del throttle compartido (evita superar el rate limit al procesar
# varias wallets seguidas).
_throttle_lock = threading.Lock()
_last_call_ts: float = 0.0


def _load_env_file() -> None:
    """Carga variables de un fichero .env en os.environ si aun no estan.

    Parser minimo (sin dependencias) para que la key del .env este disponible
    aunque no se use python-dotenv. No sobreescribe variables ya definidas.
    """
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _get_api_key() -> str:
    """Devuelve la API key o lanza un error claro (en la llamada, no al importar)."""
    key = os.getenv(POLYGONSCAN_API_KEY_ENV)
    if not key:
        _load_env_file()
        key = os.getenv(POLYGONSCAN_API_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"Falta {POLYGONSCAN_API_KEY_ENV}. Consigue una key gratuita en "
            "https://polygonscan.com/myapikey (registro gratis) y ponla en el "
            "fichero .env como POLYGONSCAN_API_KEY=tu_key, o exportala como "
            "variable de entorno."
        )
    return key


def _throttle() -> None:
    """Espera lo justo para no superar MIN_INTERVAL_SECONDS entre llamadas."""
    global _last_call_ts
    with _throttle_lock:
        elapsed = time.monotonic() - _last_call_ts
        if elapsed < MIN_INTERVAL_SECONDS:
            time.sleep(MIN_INTERVAL_SECONDS - elapsed)
        _last_call_ts = time.monotonic()


def get_first_transactions(wallet_address: str, limit: int = 10) -> list[dict[str, Any]]:
    """Primeras transacciones ENTRANTES de una wallet, mas antiguas primero.

    Usa el endpoint account/txlist ordenado ascendente y filtra las que tienen
    a `wallet_address` como destinatario (`to`). Sirve para saber quien financio
    la wallet originalmente. Reintentos y backoff los aporta `http.get_json`.

    Args:
        wallet_address: direccion de la wallet a inspeccionar.
        limit: numero maximo de transacciones entrantes a devolver.

    Returns:
        Lista de transacciones (dicts crudos de Polygonscan), orden cronologico.
    """
    params = {
        "module": "account",
        "action": "txlist",
        "address": wallet_address,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": 100,  # traemos un bloque y filtramos entrantes en el cliente
        "sort": "asc",
    }
    return _fetch_incoming(wallet_address, params, action="txlist", limit=limit)


def get_first_token_transfers(
    wallet_address: str,
    contract_address: str = USDC_CONTRACT_POLYGON,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Primeras transferencias ENTRANTES de un token ERC-20, mas antiguas primero.

    Usa account/tokentx filtrando por `contractaddress`, ordenado ascendente, y
    devuelve las que tienen a `wallet_address` como destinatario (`to`). Es el
    metodo principal para el funding source de las proxy wallets de Polymarket,
    que se financian con USDC.e (no con MATIC nativo).

    Args:
        wallet_address: direccion de la wallet a inspeccionar.
        contract_address: contrato del token (por defecto USDC.e en Polygon).
        limit: numero maximo de transferencias entrantes a devolver.

    Returns:
        Lista de transferencias (dicts crudos), orden cronologico.
    """
    params = {
        "module": "account",
        "action": "tokentx",
        "address": wallet_address,
        "contractaddress": contract_address,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": 100,
        "sort": "asc",
    }
    return _fetch_incoming(wallet_address, params, action="tokentx", limit=limit)


def _fetch_incoming(
    wallet_address: str,
    params: dict[str, Any],
    *,
    action: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Llama al endpoint, filtra las entrantes (`to == wallet`) y aplica el limite.

    Centraliza la key, el throttle de rate limit y el parseo de la respuesta
    (status "0" / result no-lista se tratan como "sin resultados").
    """
    api_key = _get_api_key()
    _throttle()

    full_params = {"chainid": POLYGON_CHAIN_ID, **params, "apikey": api_key}
    payload = get_json(POLYGONSCAN_BASE_URL, params=full_params)

    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, list):
        # status "0" con mensaje (p.ej. "No transactions found") o error de la API.
        logger.info(
            "Polygonscan %s %s: sin resultados (%s)",
            action, wallet_address,
            payload.get("message") if isinstance(payload, dict) else payload,
        )
        return []

    target = wallet_address.lower()
    incoming = [tx for tx in result if str(tx.get("to", "")).lower() == target]
    logger.info(
        "Polygonscan %s %s: %d totales, %d entrantes (limit %d)",
        action, wallet_address, len(result), len(incoming), limit,
    )
    return incoming[:limit]
