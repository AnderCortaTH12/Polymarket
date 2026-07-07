"""Tests del calculo de P&L de las alertas (funciones puras, sin Streamlit)."""
import unittest

from src.analysis.pnl import compute_pnl, format_pnl_cell


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


if __name__ == "__main__":
    unittest.main()
