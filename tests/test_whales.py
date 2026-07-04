"""Tests del cruce on-chain de ballenas, con Polygonscan mockeado.

No gastan rate limit real: se parchean `get_first_token_transfers` (USDC) y
`get_first_transactions` (nativo, fallback) para verificar que
`get_wallet_funding_source` y `group_by_funding_source` parsean e interpretan
bien la respuesta.
"""
import unittest
from unittest.mock import patch

from src.analysis.whales import (
    Whale,
    get_wallet_funding_source,
    group_by_funding_source,
    politics_portfolio_share,
)

WALLET = "0xAaAa000000000000000000000000000000000001"
FUNDER = "0xF00000000000000000000000000000000000000A"
FUNDER_NATIVE = "0xF00000000000000000000000000000000000000B"


def _tx(from_addr: str, to_addr: str, ts: str) -> dict:
    """Transferencia/transaccion cruda al estilo de Polygonscan (campos que usamos)."""
    return {"from": from_addr, "to": to_addr, "timeStamp": ts, "value": "1000"}


class TestGetWalletFundingSource(unittest.TestCase):
    """Verifica USDC como metodo principal y el fallback a tx nativa."""

    @patch("src.analysis.whales.get_first_token_transfers")
    def test_usa_usdc_como_principal(self, mock_usdc) -> None:
        mock_usdc.return_value = [_tx(FUNDER, WALLET, "1700000000")]
        self.assertEqual(get_wallet_funding_source(WALLET), (FUNDER, "usdc"))
        mock_usdc.assert_called_once_with(WALLET, limit=1)

    @patch("src.analysis.whales.get_first_transactions")
    @patch("src.analysis.whales.get_first_token_transfers")
    def test_fallback_a_nativo_si_no_hay_usdc(self, mock_usdc, mock_native) -> None:
        mock_usdc.return_value = []  # sin transferencias USDC
        mock_native.return_value = [_tx(FUNDER_NATIVE, WALLET, "1700000000")]
        self.assertEqual(get_wallet_funding_source(WALLET), (FUNDER_NATIVE, "native"))
        mock_native.assert_called_once_with(WALLET, limit=1)

    @patch("src.analysis.whales.get_first_transactions")
    @patch("src.analysis.whales.get_first_token_transfers")
    def test_sin_entradas_devuelve_none(self, mock_usdc, mock_native) -> None:
        mock_usdc.return_value = []
        mock_native.return_value = []
        self.assertIsNone(get_wallet_funding_source(WALLET))


class TestGroupByFundingSource(unittest.TestCase):
    """Verifica que solo se devuelven los clusters con mas de una wallet."""

    @patch("src.analysis.whales.get_first_transactions", return_value=[])
    @patch("src.analysis.whales.get_first_token_transfers")
    def test_agrupa_wallets_con_mismo_funder(self, mock_usdc, _mock_native) -> None:
        w1, w2, w3 = Whale("0xw1"), Whale("0xw2"), Whale("0xw3")

        # w1 y w2 comparten FUNDER (via USDC); w3 tiene otro (no forma cluster).
        sources = {
            "0xw1": [_tx(FUNDER, "0xw1", "1")],
            "0xw2": [_tx(FUNDER, "0xw2", "2")],
            "0xw3": [_tx("0xOTHER", "0xw3", "3")],
        }
        mock_usdc.side_effect = lambda addr, limit=1: sources[addr]

        clusters = group_by_funding_source([w1, w2, w3])
        self.assertEqual(clusters, {FUNDER: ["0xw1", "0xw2"]})

    @patch("src.analysis.whales.get_first_transactions", return_value=[])
    @patch("src.analysis.whales.get_first_token_transfers", return_value=[])
    def test_ignora_wallets_sin_funding(self, _mock_usdc, _mock_native) -> None:
        self.assertEqual(group_by_funding_source([Whale("0xw1"), Whale("0xw2")]), {})


class TestPoliticsPortfolioShare(unittest.TestCase):
    """Valora la parte de una cartera que esta en mercados de politica."""

    POL = {"0xpolA", "0xpolB"}

    def test_suma_politica_y_total(self) -> None:
        positions = [
            {"conditionId": "0xpolA", "currentValue": "100"},
            {"conditionId": "0xotro", "currentValue": "300"},
            {"conditionId": "0xpolB", "currentValue": "50"},
        ]
        pol, tot = politics_portfolio_share(positions, self.POL)
        self.assertEqual(pol, 150.0)
        self.assertEqual(tot, 450.0)

    def test_cero_politica_operador(self) -> None:
        # Wallet tipo operador: mucho valor pero nada en politica.
        positions = [{"conditionId": "0xotro", "currentValue": "1000000"}]
        pol, tot = politics_portfolio_share(positions, self.POL)
        self.assertEqual(pol, 0.0)
        self.assertEqual(tot, 1000000.0)

    def test_valores_no_numericos_no_rompen(self) -> None:
        positions = [{"conditionId": "0xpolA", "currentValue": None},
                     {"conditionId": "0xpolA"}]
        pol, tot = politics_portfolio_share(positions, self.POL)
        self.assertEqual((pol, tot), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
