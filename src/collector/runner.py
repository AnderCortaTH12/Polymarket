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

from src.client.gamma import flatten_markets, get_politics_events, get_political_condition_ids
from src.collector.models import connect
from src.collector.retention import purge_old_snapshots, DEFAULT_RETENTION_DAYS

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
    """Toma un snapshot: descarga, aplana e inserta solo mercados de política. Devuelve cuantos."""
    started = time.monotonic()
    ts = datetime.now(timezone.utc).isoformat()

    events = get_politics_events()
    markets = flatten_markets(events)
    politics_ids = get_political_condition_ids(events=events)

    total_markets = len([m for m in markets if m["market_id"]])
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
        if m["market_id"] and m.get("condition_id") in politics_ids
    ]
    elapsed = time.monotonic() - started
    logger.info("Snapshot %s: %d mercados totales, %d de política insertados en %.1fs",
                ts, total_markets, len(rows), elapsed)
    conn.executemany(_INSERT_SQL, rows)
    conn.commit()
    return len(rows)


def main() -> None:
    """CLI: un snapshot unico, o --loop N para repetir cada N segundos (min 30)."""
    parser = argparse.ArgumentParser(description="Collector de snapshots de Polymarket Politics")
    parser.add_argument(
        "--loop", type=int, metavar="N",
        help=f"Repetir cada N segundos (minimo {MIN_LOOP_SECONDS}). Sin esto, un solo snapshot.",
    )
    parser.add_argument(
        "--retention-days", type=int, default=DEFAULT_RETENTION_DAYS, metavar="D",
        help=f"Dias de snapshots a mantener (defecto {DEFAULT_RETENTION_DAYS}).",
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
    logger.info("Iniciando loop cada %ds (retención: %d días). Ctrl+C para parar.",
               interval, args.retention_days)
    next_purge = datetime.now(timezone.utc)
    try:
        while True:
            try:
                take_snapshot(conn)
                now = datetime.now(timezone.utc)
                if now >= next_purge:
                    purge_old_snapshots(conn, days=args.retention_days)
                    next_purge = now.replace(hour=0, minute=0, second=0, microsecond=0) + \
                                 __import__('datetime').timedelta(days=1)
            except Exception:  # noqa: BLE001 - un fallo puntual no debe matar el loop
                logger.exception("Error en loop; se reintenta en el siguiente ciclo")
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("Loop detenido por el usuario")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
