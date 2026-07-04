"""Helper HTTP compartido por los clientes de las APIs de Polymarket.

Centraliza el timeout (15s) y los reintentos con backoff exponencial exigidos
por las convenciones del proyecto, para no repetir esa lógica en cada cliente.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Convenciones del proyecto: timeout fijo y 3 reintentos con backoff exponencial.
DEFAULT_TIMEOUT: int = 15
MAX_RETRIES: int = 3
BACKOFF_BASE: float = 1.5  # segundos; espera = BACKOFF_BASE ** intento


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = MAX_RETRIES,
) -> Any:
    """Hace un GET y devuelve el JSON, reintentando con backoff exponencial.

    Reintenta ante errores de red y ante HTTP 429/5xx (rate limit o fallo del
    servidor). Otros errores HTTP (4xx) se propagan de inmediato porque
    reintentarlos no cambiaría el resultado.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(
                    f"HTTP {resp.status_code} en {url}", response=resp
                )
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            wait = BACKOFF_BASE ** attempt
            logger.warning(
                "GET %s fallo (intento %d/%d): %s. Reintento en %.1fs",
                url, attempt + 1, max_retries, exc, wait,
            )
            if attempt + 1 < max_retries:
                time.sleep(wait)
    assert last_exc is not None
    raise last_exc
