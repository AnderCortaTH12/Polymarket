"""Tests del backtest cuantitativo (funciones puras, resolver falso, sin red).

Cubre SimulatedTrade, execute_backtest con alertas fabricadas, aggregate_results,
compare_strategies, analyze_by_component y analyze_by_longshot_tier, mas los
helpers de curva de capital / drawdown y el formateo de tabla del runner.
"""
import json
import math
import unittest

import pandas as pd

from src.analysis import backtest_live_alerts as bt
from src.analysis.backtest_live_alerts import (
    ExitObservation,
    ExitStrategy,
    SimulatedTrade,
    aggregate_results,
    analyze_by_component,
    analyze_by_longshot_tier,
    compare_strategies,
    execute_backtest,
)


def make_alert(alert_id, entry, size, side="Yes", trade_side="BUY", score=60, breakdown=None, ts="2026-06-01T00:00:00+00:00"):
    """Fila de alerta fabricada (como la que devuelve la tabla `alerts`)."""
    return {
        "id": alert_id,
        "ts": ts,
        "condition_id": f"0xcond{alert_id}",
        "market_question": f"Mercado {alert_id}",
        "wallet": f"0xw{alert_id}",
        "side": side,
        "trade_side": trade_side,
        "trade_size_usd": size,
        "price_at_detection": entry,
        "score_total": score,
        "score_breakdown": json.dumps(breakdown or {}),
        "bucket_imbalance": 0.5,
    }


def const_resolver(exit_price, status="resolved"):
    """Resolver que devuelve siempre el mismo precio de salida."""
    def _r(trade, strategy):
        return ExitObservation(exit_price, status)
    return _r


class TestSimulatedTrade(unittest.TestCase):
    def test_shares(self):
        t = SimulatedTrade(1, 0.25, 1000.0, "0x", "Yes", "BUY", 60, "2026-01-01T00:00:00+00:00")
        self.assertAlmostEqual(t.shares, 4000.0)

    def test_shares_entrada_cero_no_revienta(self):
        t = SimulatedTrade(1, 0.0, 1000.0, "0x", "Yes", "BUY", 60, "ts")
        self.assertEqual(t.shares, 0.0)


class TestExecuteBacktest(unittest.TestCase):
    def test_buy_yes_gana_al_resolver_1(self):
        df = pd.DataFrame([make_alert(1, 0.40, 1000.0, "Yes", "BUY")])
        out = execute_backtest(df, ExitStrategy.RESOLUTION, const_resolver(1.0))
        row = out.iloc[0]
        # shares = 2500; pnl = (1-0.4)*2500 = 1500; pct = 150%
        self.assertAlmostEqual(row["pnl"], 1500.0)
        self.assertAlmostEqual(row["pnl_pct"], 150.0)

    def test_buy_yes_pierde_al_resolver_0(self):
        df = pd.DataFrame([make_alert(1, 0.40, 1000.0, "Yes", "BUY")])
        out = execute_backtest(df, ExitStrategy.RESOLUTION, const_resolver(0.0))
        self.assertAlmostEqual(out.iloc[0]["pnl"], -1000.0)
        self.assertAlmostEqual(out.iloc[0]["pnl_pct"], -100.0)

    def test_5_buy_yes_5_buy_no(self):
        rows = [make_alert(i, 0.50, 1000.0, "Yes", "BUY") for i in range(5)]
        rows += [make_alert(10 + i, 0.50, 1000.0, "No", "BUY") for i in range(5)]
        df = pd.DataFrame(rows)
        # No side gana si su precio va a 1 (outcome distinto, pero mismo signo aqui).
        out = execute_backtest(df, ExitStrategy.RESOLUTION, const_resolver(1.0))
        self.assertEqual(len(out), 10)
        self.assertTrue((out["pnl"] > 0).all())

    def test_status_na_produce_pnl_nan(self):
        df = pd.DataFrame([make_alert(1, 0.40, 1000.0)])
        out = execute_backtest(df, ExitStrategy.RESOLUTION, const_resolver(None, "na"))
        self.assertTrue(math.isnan(out.iloc[0]["pnl"]))
        self.assertEqual(out.iloc[0]["status"], "na")

    def test_entry_invalido_es_na(self):
        df = pd.DataFrame([make_alert(1, 0.0, 1000.0)])
        out = execute_backtest(df, ExitStrategy.RESOLUTION, const_resolver(1.0))
        self.assertTrue(math.isnan(out.iloc[0]["pnl"]))


class TestAggregate(unittest.TestCase):
    def _bt(self, exits):
        # exits: lista de (entry, size, exit_price)
        rows, resolvers = [], {}
        for i, (entry, size, ex) in enumerate(exits):
            rows.append(make_alert(i, entry, size, "Yes", "BUY"))
            resolvers[i] = ex
        df = pd.DataFrame(rows)

        def _r(trade, strategy):
            return ExitObservation(resolvers[trade.alert_id], "resolved")

        return execute_backtest(df, ExitStrategy.RESOLUTION, _r)

    def test_metrics_basicas(self):
        # dos trades: +100% y -50%
        out = self._bt([(0.50, 1000.0, 1.0), (0.50, 1000.0, 0.25)])
        agg = aggregate_results(out)
        self.assertEqual(agg["n_trades"], 2)
        self.assertAlmostEqual(agg["retorno_medio_pct"], (100.0 - 50.0) / 2)
        self.assertAlmostEqual(agg["pct_rentables"], 50.0)
        self.assertAlmostEqual(agg["max_ganancia_pct"], 100.0)
        self.assertAlmostEqual(agg["max_perdida_pct"], -50.0)
        self.assertGreater(len(agg["equity_curve"]), 0)

    def test_mediana(self):
        out = self._bt([(0.50, 100.0, 1.0), (0.50, 100.0, 0.5), (0.50, 100.0, 0.25)])
        agg = aggregate_results(out)
        # retornos: +100%, 0%, -50% -> mediana 0
        self.assertAlmostEqual(agg["retorno_mediano_pct"], 0.0)

    def test_sharpe_cero_si_sin_varianza(self):
        out = self._bt([(0.50, 100.0, 1.0), (0.50, 100.0, 1.0)])
        agg = aggregate_results(out)
        self.assertEqual(agg["sharpe"], 0.0)

    def test_sharpe_positivo(self):
        out = self._bt([(0.50, 100.0, 1.0), (0.50, 100.0, 0.75)])
        agg = aggregate_results(out)
        self.assertGreater(agg["sharpe"], 0.0)

    def test_vacio(self):
        agg = aggregate_results(pd.DataFrame())
        self.assertEqual(agg["n_trades"], 0)
        self.assertEqual(agg["equity_curve"], [])

    def test_drawdown_helper(self):
        # sube a 2, cae a 1 -> dd = 50%
        self.assertAlmostEqual(bt._max_drawdown([1.0, 2.0, 1.0]), 0.5)

    def test_equity_curve_helper(self):
        curve = bt._equity_curve([1.0, -0.5])  # +100% luego -50%
        self.assertAlmostEqual(curve[0], 2.0)
        self.assertAlmostEqual(curve[1], 1.0)


class TestCompareStrategies(unittest.TestCase):
    def test_tres_estrategias_resultados_distintos(self):
        df = pd.DataFrame([make_alert(i, 0.50, 1000.0, "Yes", "BUY") for i in range(3)])

        def _r(trade, strategy):
            # cada estrategia sale a un precio distinto
            prices = {
                ExitStrategy.RESOLUTION: 1.0,
                ExitStrategy.HORIZON_24H: 0.60,
                ExitStrategy.HORIZON_1W: 0.40,
            }
            return ExitObservation(prices[strategy], "active")

        table = compare_strategies(df, _r)
        self.assertEqual(len(table), 3)
        # RESOLUTION (mejor) debe quedar primera por retorno medio
        self.assertEqual(table.iloc[0]["estrategia"], ExitStrategy.RESOLUTION.value)
        self.assertGreater(table.iloc[0]["retorno_medio"], table.iloc[-1]["retorno_medio"])

    def test_columnas(self):
        df = pd.DataFrame([make_alert(1, 0.5, 100.0)])
        table = compare_strategies(df, const_resolver(1.0))
        for col in ("estrategia", "n_trades", "retorno_medio", "pct_rentables", "sharpe", "max_drawdown"):
            self.assertIn(col, table.columns)


class TestAnalyzeByComponent(unittest.TestCase):
    def test_agrupa_por_componente(self):
        rows = [
            make_alert(1, 0.50, 1000.0, breakdown={"wallet_fresca": 25, "longshot": 0}),
            make_alert(2, 0.50, 1000.0, breakdown={"wallet_fresca": 25, "longshot": 20}),
            make_alert(3, 0.50, 1000.0, breakdown={"wallet_fresca": 0, "longshot": 20}),
        ]
        df = pd.DataFrame(rows)
        out = analyze_by_component(df, const_resolver(1.0))
        wf = out[out["componente"] == "wallet_fresca"].iloc[0]
        ls = out[out["componente"] == "longshot"].iloc[0]
        self.assertEqual(wf["n_alertas"], 2)
        self.assertEqual(ls["n_alertas"], 2)
        # todos resuelven a 1 desde 0.5 => +100%
        self.assertAlmostEqual(wf["retorno_medio"], 100.0)

    def test_componente_sin_alertas(self):
        df = pd.DataFrame([make_alert(1, 0.5, 100.0, breakdown={"wallet_fresca": 25})])
        out = analyze_by_component(df, const_resolver(1.0))
        cluster = out[out["componente"] == "cluster"].iloc[0]
        self.assertEqual(cluster["n_alertas"], 0)


class TestAnalyzeByLongshotTier(unittest.TestCase):
    def test_agrupa_por_tramo(self):
        rows = [
            make_alert(1, 0.08, 1000.0, breakdown={"longshot": 20, "longshot_tier": 0.10}),
            make_alert(2, 0.15, 1000.0, breakdown={"longshot": 20, "longshot_tier": 0.20}),
            make_alert(3, 0.50, 1000.0, breakdown={"longshot": 0, "longshot_tier": None}),
        ]
        df = pd.DataFrame(rows)
        out = analyze_by_longshot_tier(df, const_resolver(1.0))
        tramos = set(out["tramo"])
        self.assertIn("<= 0.10", tramos)
        self.assertIn("<= 0.20", tramos)
        self.assertIn("sin longshot", tramos)
        for _, r in out.iterrows():
            self.assertEqual(r["n_alertas"], 1)

    def test_vacio(self):
        out = analyze_by_longshot_tier(pd.DataFrame(), const_resolver(1.0))
        self.assertTrue(out.empty)


class TestParsers(unittest.TestCase):
    def test_parse_breakdown_json(self):
        self.assertEqual(bt._parse_breakdown('{"a": 1}'), {"a": 1})

    def test_parse_breakdown_invalido(self):
        self.assertEqual(bt._parse_breakdown("no-json"), {})
        self.assertEqual(bt._parse_breakdown(None), {})

    def test_parse_ts(self):
        dt = bt._parse_ts("2026-06-01T12:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)

    def test_parse_ts_invalido(self):
        self.assertIsNone(bt._parse_ts("nope"))
        self.assertIsNone(bt._parse_ts(""))

    def test_price_at(self):
        series = [{"t": 100, "p": 0.1}, {"t": 200, "p": 0.2}, {"t": 300, "p": 0.3}]
        from datetime import datetime, timezone
        target = datetime.fromtimestamp(250, timezone.utc)
        self.assertAlmostEqual(bt._price_at(series, target), 0.2)


if __name__ == "__main__":
    unittest.main()
