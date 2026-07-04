"""Loop principal del collector: toma snapshots de los mercados a SQLite.

Un snapshot llama al Gamma API, aplana a una fila por mercado y los inserta con
timestamp UTC. Se puede ejecutar una sola vez o en bucle cada N segundos.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone

from src.client.gamma import flatten_markets, get_politics_events
from src.collector.models import connect

logger = logging.getLogger(__name__)

MIN_LOOP_SECONDS: int = 30  # Gamma cachea 30-60s; no tiene sentido ir mas rapido.

_INSERT_SQL = """
INSERT OR REPLACE INTO snapshots (
    ts, market_id, condition_id, event_slug, question,
    outcomes, outcome_prices, best_bid, best_ask, spread,
    last_trade_price, volume_24h, volume_total, liquidity, end_date
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def take_snapshot(conn: sqlite3.Connection) -> int:
    """Toma un snapshot: descarga, aplana e inserta los mercados. Devuelve cuantos."""
    started = time.monotonic()
    ts = datetime.now(timezone.utc).isoformat()

    events = get_politics_events()
    markets = flatten_markets(events)

    rows = [
        (
            ts,
            m["market_id"],
            m["condition_id"],
            m["event_slug"],
            m["question"],
            json.dumps(m["outcomes"]),
            json.dumps(m["outcome_prices"]),
            m["best_bid"],
            m["best_ask"],
            m["spread"],
            m["last_trade_price"],
            m["volume_24h"],
            m["volume_total"],
            m["liquidity"],
            m["end_date"],
        )
        for m in markets
        if m["market_id"]
    ]
    conn.executemany(_INSERT_SQL, rows)
    conn.commit()

    elapsed = time.monotonic() - started
    logger.info("Snapshot %s: %d mercados insertados en %.1fs", ts, len(rows), elapsed)
    return len(rows)


def main() -> None:
    """CLI: un snapshot unico, o --loop N para repetir cada N segundos (min 30)."""
    parser = argparse.ArgumentParser(description="Collector de snapshots de Polymarket Politics")
    parser.add_argument(
        "--loop", type=int, metavar="N",
        help=f"Repetir cada N segundos (minimo {MIN_LOOP_SECONDS}). Sin esto, un solo snapshot.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = connect()

    if args.loop is None:
        take_snapshot(conn)
        conn.close()
        return

    interval = max(args.loop, MIN_LOOP_SECONDS)
    if interval != args.loop:
        logger.warning("Intervalo elevado a %ds (minimo permitido)", interval)
    logger.info("Iniciando loop cada %ds. Ctrl+C para parar.", interval)
    try:
        while True:
            try:
                take_snapshot(conn)
            except Exception:  # noqa: BLE001 - un fallo puntual no debe matar el loop
                logger.exception("Error tomando snapshot; se reintenta en el siguiente ciclo")
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("Loop detenido por el usuario")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
