"""Tests del cruce on-chain de ballenas, con Polygonscan mockeado.

No gastan rate limit real: se parchea `get_first_transactions` para verificar
que `get_wallet_funding_source` y `group_by_funding_source` parsean e
interpretan bien la respuesta.
"""
import unittest
from unittest.mock import patch

from src.analysis.whales import (
    Whale,
    get_wallet_funding_source,
    group_by_funding_source,
)

WALLET = "0xAaAa000000000000000000000000000000000001"
FUNDER = "0xF00000000000000000000000000000000000000A"


def _tx(from_addr: str, to_addr: str, ts: str) -> dict:
    """Transaccion cruda al estilo de Polygonscan (solo los campos que usamos)."""
    return {"from": from_addr, "to": to_addr, "timeStamp": ts, "value": "1000"}


class TestGetWalletFundingSource(unittest.TestCase):
    """Verifica la extraccion del remitente de la primera tx entrante."""

    @patch("src.analysis.whales.get_first_transactions")
    def test_devuelve_remitente_de_primera_entrante(self, mock_txs) -> None:
        mock_txs.return_value = [_tx(FUNDER, WALLET, "1700000000")]
        self.assertEqual(get_wallet_funding_source(WALLET), FUNDER)
        mock_txs.assert_called_once_with(WALLET, limit=1)

    @patch("src.analysis.whales.get_first_transactions")
    def test_sin_transacciones_entrantes_devuelve_none(self, mock_txs) -> None:
        mock_txs.return_value = []
        self.assertIsNone(get_wallet_funding_source(WALLET))


class TestGroupByFundingSource(unittest.TestCase):
    """Verifica que solo se devuelven los clusters con mas de una wallet."""

    @patch("src.analysis.whales.get_first_transactions")
    def test_agrupa_wallets_con_mismo_funder(self, mock_txs) -> None:
        w1 = Whale(proxy_wallet="0xw1")
        w2 = Whale(proxy_wallet="0xw2")
        w3 = Whale(proxy_wallet="0xw3")

        # w1 y w2 comparten FUNDER; w3 tiene otro (no forma cluster).
        sources = {
            "0xw1": [_tx(FUNDER, "0xw1", "1")],
            "0xw2": [_tx(FUNDER, "0xw2", "2")],
            "0xw3": [_tx("0xOTHER", "0xw3", "3")],
        }
        mock_txs.side_effect = lambda addr, limit=1: sources[addr]

        clusters = group_by_funding_source([w1, w2, w3])
        self.assertEqual(clusters, {FUNDER: ["0xw1", "0xw2"]})

    @patch("src.analysis.whales.get_first_transactions")
    def test_ignora_wallets_sin_funding(self, mock_txs) -> None:
        w1 = Whale(proxy_wallet="0xw1")
        w2 = Whale(proxy_wallet="0xw2")
        mock_txs.side_effect = lambda addr, limit=1: []  # ninguna con entrantes
        self.assertEqual(group_by_funding_source([w1, w2]), {})


if __name__ == "__main__":
    unittest.main()
