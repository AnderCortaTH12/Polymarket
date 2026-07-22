"""Tests para el histórico de trades por wallet."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.analysis.wallet_trades import (
    ensure_wallet_trades_schema,
    upsert_wallet_trade,
    get_wallet_exit,
)
from src.analysis.wallet_trades_sync import (
    ensure_wallet_sync_metadata_schema,
    backfill_wallet,
    should_refetch_wallet,
    WALLET_TRADES_TTL_HOURS,
)


class TestWalletTrades(unittest.TestCase):
    """Tests para la tabla wallet_trades y operaciones básicas."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.conn = sqlite3.connect(str(self.db_path))
        ensure_wallet_trades_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmpdir.cleanup()

    def test_upsert_wallet_trade(self) -> None:
        """Verifica que se inserta un trade correctamente."""
        upsert_wallet_trade(
            self.conn,
            wallet="0xWALLET1",
            condition_id="0xMARKET1",
            transaction_hash="0xTX1",
            ts="2026-07-22T10:00:00+00:00",
            side="Yes",
            trade_side="BUY",
            price=0.50,
            size_shares=1000.0,
            size_usd=500.0,
            outcome_index=0,
        )
        cursor = self.conn.execute("SELECT COUNT(*) FROM wallet_trades")
        self.assertEqual(cursor.fetchone()[0], 1)

    def test_upsert_wallet_trade_idempotencia(self) -> None:
        """Verifica que no inserta duplicados por transaction_hash."""
        tx_hash = "0xTX1"
        for _ in range(3):
            upsert_wallet_trade(
                self.conn,
                wallet="0xWALLET1",
                condition_id="0xMARKET1",
                transaction_hash=tx_hash,
                ts="2026-07-22T10:00:00+00:00",
                side="Yes",
                trade_side="BUY",
                price=0.50,
                size_shares=1000.0,
                size_usd=500.0,
                outcome_index=0,
            )
        cursor = self.conn.execute("SELECT COUNT(*) FROM wallet_trades")
        self.assertEqual(cursor.fetchone()[0], 1, "No debe insertar duplicados")


class TestGetWalletExit(unittest.TestCase):
    """Tests para get_wallet_exit: detección de cuando la ballena vendió."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.conn = sqlite3.connect(str(self.db_path))
        ensure_wallet_trades_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmpdir.cleanup()

    def test_get_wallet_exit_encuentra_sell_posterior(self) -> None:
        """Verifica que encuentra el primer SELL después de after_ts."""
        wallet = "0xWALLET1"
        market = "0xMARKET1"

        # Inserta BUY primero, luego SELL.
        upsert_wallet_trade(self.conn, wallet, market, "0xTX1",
                           "2026-07-22T10:00:00+00:00", "Yes", "BUY", 0.50, 1000.0, 500.0, 0)
        upsert_wallet_trade(self.conn, wallet, market, "0xTX2",
                           "2026-07-22T11:00:00+00:00", "Yes", "SELL", 0.60, 800.0, 480.0, 0)

        exit_trade = get_wallet_exit(self.conn, wallet, market, "2026-07-22T10:30:00+00:00")
        self.assertIsNotNone(exit_trade, "Debe encontrar el SELL")
        self.assertEqual(exit_trade["transaction_hash"], "0xTX2")
        self.assertEqual(exit_trade["trade_side"], "SELL")

    def test_get_wallet_exit_devuelve_none_sin_sell(self) -> None:
        """Verifica que devuelve None si no hay SELL posterior."""
        wallet = "0xWALLET1"
        market = "0xMARKET1"

        # Solo inserta BUY, no SELL.
        upsert_wallet_trade(self.conn, wallet, market, "0xTX1",
                           "2026-07-22T10:00:00+00:00", "Yes", "BUY", 0.50, 1000.0, 500.0, 0)

        exit_trade = get_wallet_exit(self.conn, wallet, market, "2026-07-22T09:00:00+00:00")
        self.assertIsNone(exit_trade, "Debe devolver None si no hay SELL")

    def test_get_wallet_exit_ignora_sell_anterior(self) -> None:
        """Verifica que ignora SELL anteriores a after_ts."""
        wallet = "0xWALLET1"
        market = "0xMARKET1"

        # Inserta SELL antiguo y SELL posterior.
        upsert_wallet_trade(self.conn, wallet, market, "0xTX1",
                           "2026-07-22T08:00:00+00:00", "Yes", "SELL", 0.40, 500.0, 200.0, 0)
        upsert_wallet_trade(self.conn, wallet, market, "0xTX2",
                           "2026-07-22T11:00:00+00:00", "Yes", "SELL", 0.60, 800.0, 480.0, 0)

        # Pide SELL después de las 09:00.
        exit_trade = get_wallet_exit(self.conn, wallet, market, "2026-07-22T09:00:00+00:00")
        self.assertIsNotNone(exit_trade, "Debe encontrar SELL")
        self.assertEqual(exit_trade["transaction_hash"], "0xTX2")


class TestBackfillWallet(unittest.TestCase):
    """Tests para backfill_wallet: sincronización desde Data API."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.conn = sqlite3.connect(str(self.db_path))
        ensure_wallet_trades_schema(self.conn)
        ensure_wallet_sync_metadata_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmpdir.cleanup()

    @patch("src.analysis.wallet_trades_sync.get_user_trades")
    def test_backfill_wallet_inserta_trades(self, mock_get_trades) -> None:
        """Verifica que backfill inserta trades de la Data API."""
        mock_get_trades.return_value = [
            {
                "transactionHash": "0xTX1",
                "conditionId": "0xMKT1",
                "timestamp": "2026-07-22T10:00:00+00:00",
                "outcome": "Yes",
                "side": "BUY",
                "price": 0.50,
                "size": 1000.0,
                "outcomeIndex": 0,
            },
            {
                "transactionHash": "0xTX2",
                "conditionId": "0xMKT1",
                "timestamp": "2026-07-22T11:00:00+00:00",
                "outcome": "Yes",
                "side": "SELL",
                "price": 0.60,
                "size": 800.0,
                "outcomeIndex": 0,
            },
        ]

        inserted = backfill_wallet(self.conn, "0xWALLET1")
        self.assertEqual(inserted, 2, "Debe insertar 2 trades nuevos")

        cursor = self.conn.execute("SELECT COUNT(*) FROM wallet_trades")
        self.assertEqual(cursor.fetchone()[0], 2)

    @patch("src.analysis.wallet_trades_sync.get_user_trades")
    def test_backfill_wallet_idempotencia(self, mock_get_trades) -> None:
        """Verifica que no duplica trades si se llama dos veces."""
        trades = [
            {
                "transactionHash": "0xTX1",
                "conditionId": "0xMKT1",
                "timestamp": "2026-07-22T10:00:00+00:00",
                "outcome": "Yes",
                "side": "BUY",
                "price": 0.50,
                "size": 1000.0,
                "outcomeIndex": 0,
            },
        ]
        mock_get_trades.return_value = trades

        backfill_wallet(self.conn, "0xWALLET1")
        backfill_wallet(self.conn, "0xWALLET1")

        cursor = self.conn.execute("SELECT COUNT(*) FROM wallet_trades")
        self.assertEqual(cursor.fetchone()[0], 1, "No debe duplicar")

    @patch("src.analysis.wallet_trades_sync.get_user_trades")
    def test_backfill_wallet_registra_sync(self, mock_get_trades) -> None:
        """Verifica que registra el timestamp de último sync."""
        mock_get_trades.return_value = []

        backfill_wallet(self.conn, "0xWALLET1")

        cursor = self.conn.execute(
            "SELECT last_sync_ts FROM wallet_trades_sync WHERE wallet = ?",
            ("0xWALLET1",)
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row, "Debe registrar el sync")


class TestShouldRefetchWallet(unittest.TestCase):
    """Tests para should_refetch_wallet: lógica de TTL."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.conn = sqlite3.connect(str(self.db_path))
        ensure_wallet_trades_schema(self.conn)
        ensure_wallet_sync_metadata_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmpdir.cleanup()

    def test_refetch_sin_sync_previo(self) -> None:
        """Debe devolver True si nunca se sincronizó."""
        result = should_refetch_wallet(self.conn, "0xWALLET1")
        self.assertTrue(result, "Debe requerir sync inicial")

    def test_no_refetch_sync_reciente(self) -> None:
        """Debe devolver False si el sync es reciente."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO wallet_trades_sync (wallet, last_sync_ts) VALUES (?, ?)",
            ("0xWALLET1", now)
        )
        self.conn.commit()

        result = should_refetch_wallet(self.conn, "0xWALLET1", ttl_hours=24.0)
        self.assertFalse(result, "Sync reciente no debe requerir refetch")

    def test_refetch_sync_antiguo(self) -> None:
        """Debe devolver True si el sync es más viejo que TTL."""
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        self.conn.execute(
            "INSERT INTO wallet_trades_sync (wallet, last_sync_ts) VALUES (?, ?)",
            ("0xWALLET1", old)
        )
        self.conn.commit()

        result = should_refetch_wallet(self.conn, "0xWALLET1", ttl_hours=24.0)
        self.assertTrue(result, "Sync antiguo debe requerir refetch")


if __name__ == "__main__":
    unittest.main()
