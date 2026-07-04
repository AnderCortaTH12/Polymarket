"""Tests del cliente Gamma con unittest de la stdlib (no necesita internet).

Verifica que `flatten_markets` parsea correctamente los campos que Polymarket
entrega como strings JSON.
"""
import unittest

from src.client.gamma import flatten_markets


class TestFlattenMarkets(unittest.TestCase):
    """Comprueba el aplanado y parseo de un evento con campos como string JSON."""

    def _fake_event(self) -> dict:
        """Evento falso con outcomes/outcomePrices/clobTokenIds como strings JSON."""
        return {
            "slug": "elecciones-2028",
            "title": "¿Quién ganará en 2028?",
            "markets": [
                {
                    "id": "0xMARKET",
                    "conditionId": "0xCOND",
                    "question": "¿Ganará el candidato A?",
                    "active": True,
                    "closed": False,
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.62", "0.38"]',
                    "clobTokenIds": '["111", "222"]',
                    "bestBid": "0.61",
                    "bestAsk": "0.63",
                    "spread": "0.02",
                    "lastTradePrice": "0.62",
                    "volume24hr": "1500.5",
                    "volume": "100000",
                    "liquidity": "5000",
                    "endDate": "2028-11-07T00:00:00Z",
                }
            ],
        }

    def test_parsea_strings_json(self) -> None:
        rows = flatten_markets([self._fake_event()])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["outcomes"], ["Yes", "No"])
        self.assertEqual(row["outcome_prices"], [0.62, 0.38])
        self.assertEqual(row["clob_token_ids"], ["111", "222"])

    def test_convierte_numericos_a_float(self) -> None:
        row = flatten_markets([self._fake_event()])[0]
        self.assertEqual(row["volume_24h"], 1500.5)
        self.assertEqual(row["spread"], 0.02)
        self.assertIsInstance(row["outcome_prices"][0], float)

    def test_campos_meta(self) -> None:
        row = flatten_markets([self._fake_event()])[0]
        self.assertEqual(row["market_id"], "0xMARKET")
        self.assertEqual(row["condition_id"], "0xCOND")
        self.assertEqual(row["event_slug"], "elecciones-2028")
        self.assertEqual(row["url"], "https://polymarket.com/event/elecciones-2028")

    def test_valores_ausentes_no_rompen(self) -> None:
        # only_tradeable=False para no filtrar el mercado de prueba (sin flags).
        row = flatten_markets([{"slug": "x", "markets": [{"id": "1"}]}], only_tradeable=False)[0]
        self.assertEqual(row["outcomes"], [])
        self.assertEqual(row["outcome_prices"], [])
        self.assertIsNone(row["volume_24h"])


class TestOnlyTradeableFilter(unittest.TestCase):
    """El filtro por defecto descarta sub-mercados cerrados o inactivos."""

    def _event_con(self, active, closed) -> dict:
        return {"slug": "e", "markets": [{"id": "m", "active": active, "closed": closed}]}

    def test_descarta_cerrados_e_inactivos(self) -> None:
        self.assertEqual(flatten_markets([self._event_con(True, True)]), [])   # cerrado
        self.assertEqual(flatten_markets([self._event_con(False, False)]), [])  # inactivo
        self.assertEqual(len(flatten_markets([self._event_con(True, False)])), 1)  # tradeable

    def test_acepta_flags_como_string(self) -> None:
        # Gamma puede devolver los flags como strings 'true'/'false'.
        self.assertEqual(len(flatten_markets([self._event_con("true", "false")])), 1)
        self.assertEqual(flatten_markets([self._event_con("true", "true")]), [])

    def test_only_tradeable_false_no_filtra(self) -> None:
        rows = flatten_markets([self._event_con(True, True)], only_tradeable=False)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
