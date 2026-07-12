"""Tests del cliente Data API relativos al orden de posiciones (Fase 1b).

El orden por defecto (CURRENT/DESC) es el que consumen el dashboard y whales.py
para mostrar las mayores posiciones; NO debe cambiar. El perfilado usa un orden
neutral aparte para no sesgar el win_rate.
"""
import unittest
from unittest.mock import patch

from src.client.data_api import NEUTRAL_SORT_BY, get_user_positions


class TestGetUserPositionsOrder(unittest.TestCase):
    def test_default_es_current_desc(self) -> None:
        with patch("src.client.data_api.get_json", return_value=[]) as mock_get:
            get_user_positions("0xW", max_positions=10)
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["sortBy"], "CURRENT")
        self.assertEqual(params["sortDirection"], "DESC")

    def test_orden_neutral_explicito(self) -> None:
        with patch("src.client.data_api.get_json", return_value=[]) as mock_get:
            get_user_positions("0xW", max_positions=10, sort_by=NEUTRAL_SORT_BY)
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["sortBy"], NEUTRAL_SORT_BY)


if __name__ == "__main__":
    unittest.main()
