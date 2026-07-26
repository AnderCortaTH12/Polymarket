"""Tests del calculo de P&L de las alertas (funciones puras, sin Streamlit)."""
import unittest

import pandas as pd

from src import config
from src.analysis.pnl import compute_pnl, format_pnl_cell
from src.dashboard.version_filter import (
    ALL_LABEL,
    apply_version_filter,
    compatible_label,
    version_filter_options,
)


class TestComputePnl(unittest.TestCase):
    def test_buy_yes_sube_gana(self) -> None:
        # buy Yes a 0.30, precio actual 0.50 -> +66.7%
        pnl, pct = compute_pnl(0.30, 3000.0, "BUY", 0.50)
        self.assertAlmostEqual(pct, 66.666, places=2)
        # shares = 3000/0.30 = 10000; pnl = 0.20 * 10000 = 2000
        self.assertAlmostEqual(pnl, 2000.0)

    def test_buy_baja_pierde(self) -> None:
        pnl, pct = compute_pnl(0.50, 1000.0, "BUY", 0.40)
        self.assertLess(pnl, 0)
        self.assertAlmostEqual(pct, -20.0)

    def test_sell_invierte(self) -> None:
        # vender y que el precio baje = ganancia
        pnl, pct = compute_pnl(0.50, 1000.0, "SELL", 0.40)
        self.assertGreater(pnl, 0)
        self.assertAlmostEqual(pct, 20.0)


class TestFormatPnlCell(unittest.TestCase):
    def test_ganancia_formato_y_signo(self) -> None:
        s = format_pnl_cell(0.30, 3000.0, "BUY", {"status": "active", "price": 0.50})
        self.assertTrue(s.startswith("+$"))
        self.assertIn("+66.7%", s)

    def test_perdida_formato(self) -> None:
        s = format_pnl_cell(0.50, 1000.0, "BUY", {"status": "active", "price": 0.40})
        self.assertTrue(s.startswith("-$"))
        self.assertIn("-20.0%", s)

    def test_cero(self) -> None:
        s = format_pnl_cell(0.50, 1000.0, "BUY", {"status": "active", "price": 0.50})
        self.assertEqual(s, "$0.00 (0%)")

    def test_resuelto_ganado(self) -> None:
        # 1000$ a 0.10 = 10000 shares; resuelto a 1 => payout 10000
        s = format_pnl_cell(0.10, 1000.0, "BUY", {"status": "resolved", "price": 1.0})
        self.assertEqual(s, "Resuelto ($10,000)")

    def test_resuelto_perdido(self) -> None:
        s = format_pnl_cell(0.10, 1000.0, "BUY", {"status": "resolved", "price": 0.0})
        self.assertEqual(s, "Resuelto ($0)")

    def test_na_si_estado_na(self) -> None:
        self.assertEqual(format_pnl_cell(0.10, 1000.0, "BUY", {"status": "na", "price": None}), "N/A")

    def test_na_si_precio_deteccion_invalido(self) -> None:
        self.assertEqual(format_pnl_cell(0, 1000.0, "BUY", {"status": "active", "price": 0.5}), "N/A")
        self.assertEqual(format_pnl_cell(None, 1000.0, "BUY", {"status": "active", "price": 0.5}), "N/A")


class TestVersionFilter(unittest.TestCase):
    """Filtro de version del dashboard: compatibles (default), una version
    concreta, o todas. Mismo criterio que backtest_runner.load_alerts, sin
    hardcodear ninguna version."""

    def _alerts(self, versions: list[str]) -> pd.DataFrame:
        return pd.DataFrame({
            "scoring_version": versions,
            "score_total": list(range(len(versions))),
        })

    def test_compatible_label_usa_config_no_literal(self) -> None:
        self.assertEqual(config.SCORING_COMPATIBLE_VERSIONS, ["v5", "v6"])
        self.assertEqual(compatible_label(), "Compatibles (v5+v6)")
        self.assertEqual(compatible_label(["v7", "v8"]), "Compatibles (v7+v8)")

    def test_version_filter_options_incluye_compatibles_datos_y_todas(self) -> None:
        options = version_filter_options(["v5", "v6", "v4"])
        self.assertEqual(options[0], compatible_label())
        self.assertEqual(options[-1], ALL_LABEL)
        self.assertIn("v4", options)
        self.assertIn("v5", options)
        self.assertIn("v6", options)

    def test_apply_version_filter_compatibles_incluye_v5_y_v6(self) -> None:
        df = self._alerts(["v4", "v5", "v6"])
        filtered, excluded = apply_version_filter(df, compatible_label())
        self.assertEqual(sorted(filtered["scoring_version"].tolist()), ["v5", "v6"])
        self.assertEqual(excluded, 1)  # solo la v4

    def test_apply_version_filter_version_concreta(self) -> None:
        df = self._alerts(["v5", "v6", "v6"])
        filtered, excluded = apply_version_filter(df, "v6")
        self.assertEqual(len(filtered), 2)
        self.assertTrue((filtered["scoring_version"] == "v6").all())
        self.assertEqual(excluded, 1)  # la v5

    def test_apply_version_filter_todas_no_excluye_nada(self) -> None:
        df = self._alerts(["v1", "v5", "v6"])
        filtered, excluded = apply_version_filter(df, ALL_LABEL)
        self.assertEqual(len(filtered), 3)
        self.assertEqual(excluded, 0)

    def test_apply_version_filter_df_vacio(self) -> None:
        df = self._alerts([])
        filtered, excluded = apply_version_filter(df, compatible_label())
        self.assertTrue(filtered.empty)
        self.assertEqual(excluded, 0)


if __name__ == "__main__":
    unittest.main()
