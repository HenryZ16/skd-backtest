"""End-to-end acceptance with hand-computed prices and real components."""

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

import pandas as pd

from skd_backtest import BacktestEngine, CostConfig, DataCapabilities, FeeScheduleEntry, OptimizerConfig, ReferenceSources
from skd_backtest.schemas import METRIC_NAMES, RESULT_COLUMNS, SOURCE_COLUMNS


class FinancialIntegrationTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.dates = [20180102, 20180103, 20180104, 20180105, 20180108, 20180109]
        prices = {
            "SH600000": [(10, 10), (10, 11), (12, 12), (12, 15), (13, 13), (14, 14)],
            "SZ000001": [(20, 20), (20, 20), (20, 20), (20, 22), (25, 25), (26, 26)],
        }
        for name, columns in SOURCE_COLUMNS.items():
            rows = []
            for i, day in enumerate(self.dates):
                for code, values in prices.items():
                    opening, close = values[i]
                    row = dict.fromkeys(columns, 1.0)
                    row.update({"日期": day, "代码": code, "名称": code})
                    if name == "MarketData":
                        row.update(open=opening, close=close, high=max(opening, close),
                                   low=min(opening, close), is_suspend=False)
                    rows.append(row)
            folder = self.root / name / "2018" / "01"
            folder.mkdir(parents=True)
            pd.DataFrame(rows).to_parquet(folder / "201801.parquet", index=False)
        self.model_dates = []

    def inference(self, *, as_of_date, data):
        self.model_dates.append(as_of_date)
        self.assertNotIn("raw_open", data["MarketData"])
        self.assertNotIn("upper_limit", data["MarketData"])
        for table in data.values():
            self.assertLessEqual(table["日期"].max(), int(as_of_date.replace("-", "")))
        return pd.DataFrame({
            "date": as_of_date, "code": ["SH600000", "SZ000001"],
            "score": [2.0, 1.0] if as_of_date == "2018-01-02" else [1.0, 2.0],
        })

    def engine(self, **options):
        config = dict(data_dir=self.root, start_date="2018-01-02", end_date="2018-01-05",
                      initial_cash=1000.0, inference=self.inference,
                      lookback=1, holding_period=1, rebalance_interval=2,
                      optimizer_config=OptimizerConfig(top_k=1))
        config.update(options)
        return BacktestEngine(**config)

    def test_real_components_match_hand_computed_adjusted_portfolio(self):
        engine = self.engine(output_dir=self.root / "output")
        metrics = engine.run()
        equity = engine.tables["equity_curve"]
        self.assertEqual(equity.date.tolist(),
                         ["2018-01-02", "2018-01-03", "2018-01-04", "2018-01-05"])
        for actual, expected in zip(equity.portfolio_value, [1000.0, 1100.0, 1200.0, 1320.0]):
            self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(metrics["total_return"], 0.32)
        self.assertAlmostEqual(metrics["turnover"], 3.0)
        self.assertEqual(metrics["transaction_cost"], 0.0)
        self.assertEqual(metrics["failed_orders"], 0)
        self.assertEqual(set(metrics), set(METRIC_NAMES))
        self.assertIsNone(metrics["annualized_excess_return"])
        self.assertTrue(equity.benchmark_nav.isna().all())
        self.assertEqual(engine.account["total_shares"], {})
        self.assertAlmostEqual(engine.account["position_values"]["SZ000001"], 1320.0)
        self.assertAlmostEqual(engine.account["cash"], 0.0)
        self.assertEqual(self.model_dates, ["2018-01-02", "2018-01-04"])

        trades = engine.tables["trades"]
        self.assertEqual(list(zip(trades.date, trades.side, trades.code)), [
            ("2018-01-03", "BUY", "SH600000"),
            ("2018-01-05", "SELL", "SH600000"),
            ("2018-01-05", "BUY", "SZ000001"),
        ])
        self.assertTrue(trades.shares.isna().all())
        self.assertTrue(trades.price.isna().all())
        self.assertEqual(engine.tables["orders"].status.tolist(), ["FILLED"] * 3)
        predictions = engine.tables["predictions"].set_index(["date", "code"])
        for key, expected in {
            ("2018-01-02", "SH600000"): 0.2,
            ("2018-01-02", "SZ000001"): 0.0,
            ("2018-01-04", "SH600000"): 13 / 12 - 1,
            ("2018-01-04", "SZ000001"): 0.25,
        }.items():
            self.assertAlmostEqual(predictions.loc[key, "future_return"], expected)
        self.assertAlmostEqual(metrics["mean_rankic"], 1.0)
        self.assertAlmostEqual(metrics["rankic_std"], 0.0)
        self.assertIsNone(metrics["rankic_ir"])
        output = self.root / "output"
        expected_files = {"metrics.json", "run.log", *(name + ".csv" for name in RESULT_COLUMNS)}
        self.assertEqual({p.name for p in output.iterdir()}, expected_files)
        self.assertEqual(json.loads((output / "metrics.json").read_text(encoding="utf-8")), metrics)
        for name, columns in RESULT_COLUMNS.items():
            exported = pd.read_csv(output / (name + ".csv"))
            self.assertEqual(exported.columns.tolist(), list(columns))
            self.assertEqual(len(exported), len(engine.tables[name]))

    def test_fees_and_slippage_match_cash_and_asset_accounting(self):
        costs = CostConfig(
            commission_rate=0.01, slippage=0.01,
            fee_schedule=(FeeScheduleEntry("2018-01-01", 0.02, 0.03),),
        )
        engine = self.engine(cost_config=costs)
        engine.run()
        first_asset = 1000 / (1.01 * 1.04)
        sold_asset = first_asset * 1.2
        sell_cash = sold_asset * 0.99 * 0.94
        final_asset = sell_cash / (1.01 * 1.04)
        self.assertAlmostEqual(engine.account["portfolio_value"], final_asset * 1.1)
        self.assertGreaterEqual(engine.account["cash"], 0.0)
        self.assertAlmostEqual(engine.account["cash"], 0.0, places=7)
        trades = engine.tables["trades"]
        expected_assets = [first_asset, sold_asset, final_asset]
        expected_cash_values = [first_asset * 1.01, sold_asset * 0.99, final_asset * 1.01]
        for actual, expected in zip(trades.position_value, expected_assets):
            self.assertAlmostEqual(actual, expected)
        for actual, expected in zip(trades.trade_value, expected_cash_values):
            self.assertAlmostEqual(actual, expected)
        expected_cost = (expected_cash_values[0] * 0.04
                         + expected_cash_values[1] * 0.06
                         + expected_cash_values[2] * 0.04)
        self.assertAlmostEqual(engine.metrics["transaction_cost"], expected_cost)
        self.assertAlmostEqual(trades.total_cost.sum(), expected_cost)
        self.assertEqual(engine.metrics["failed_orders"], 0)

    def test_raw_execution_uses_real_prices_and_share_locks(self):
        market_path = self.root / "MarketData" / "2018" / "01" / "201801.parquet"
        market = pd.read_parquet(market_path)
        for price in ("open", "high", "low", "close"):
            market["raw_" + price] = market[price] / 2
        market["upper_limit"] = market.raw_open * 1.1
        market["lower_limit"] = market.raw_open * 0.9
        market.to_parquet(market_path, index=False)
        engine = self.engine(
            initial_cash=10000.0, price_mode="raw_price",
            data_capabilities=DataCapabilities(raw_prices=True, price_limits=True),
        )
        engine.run()
        self.assertAlmostEqual(engine.account["portfolio_value"], 13200.0)
        self.assertAlmostEqual(engine.account["cash"], 0.0)
        self.assertEqual(engine.account["total_shares"], {"SZ000001": 1200})
        self.assertEqual(engine.account["sellable_shares"], {"SZ000001": 0})
        trades = engine.tables["trades"]
        self.assertEqual(trades.shares.tolist(), [2000, 2000, 1200])
        self.assertEqual(trades.price.tolist(), [5.0, 6.0, 10.0])
        self.assertAlmostEqual(engine.metrics["total_return"], 0.32)
        self.assertAlmostEqual(engine.metrics["turnover"], 3.0)

    def test_benchmark_is_external_and_reference_sources_reload_each_run(self):
        path = self.root / "benchmark.parquet"
        benchmark = pd.DataFrame({
            "date": ["2018-01-02", "2018-01-03", "2018-01-04", "2018-01-05"],
            "benchmark_return": [0.01] * 4,
        })
        benchmark.to_parquet(path, index=False)
        engine = self.engine(
            benchmark_mode="csi300",
            data_capabilities=DataCapabilities(benchmark_returns=True),
            reference_sources=ReferenceSources(benchmark_returns=path),
        )
        engine.run()
        first = engine.tables["equity_curve"]
        self.assertAlmostEqual(first.iloc[-1].benchmark_nav, 1.01 ** 4)
        self.assertAlmostEqual(first.iloc[0].active_return, -0.01)
        benchmark.loc[0, "benchmark_return"] = 0.02
        benchmark.to_parquet(path, index=False)
        engine.run()
        second = engine.tables["equity_curve"]
        self.assertAlmostEqual(second.iloc[-1].benchmark_nav, 1.02 * 1.01 ** 3)
        self.assertAlmostEqual(second.iloc[0].active_return, -0.02)
        self.assertAlmostEqual(first.iloc[-1].benchmark_nav, 1.01 ** 4)

    def test_empty_range_exports_complete_empty_results(self):
        output = self.root / "empty-output"
        engine = self.engine(start_date="2018-01-06", end_date="2018-01-07", output_dir=output)
        metrics = engine.run()
        self.assertEqual(engine.trading_dates, [])
        self.assertEqual(engine.account["portfolio_value"], 1000.0)
        for key, value in metrics.items():
            if key in ("turnover", "transaction_cost", "failed_orders"):
                self.assertEqual(value, 0)
            else:
                self.assertIsNone(value, key)
        for name, columns in RESULT_COLUMNS.items():
            self.assertTrue(engine.tables[name].empty, name)
            self.assertEqual(pd.read_csv(output / (name + ".csv")).columns.tolist(), list(columns))
        self.assertEqual(len(list(output.iterdir())), 9)

    def test_single_day_has_cash_nav_without_signal_or_sample_variance(self):
        engine = self.engine(end_date="2018-01-02")
        metrics = engine.run()
        self.assertEqual(self.model_dates, [])
        self.assertEqual(engine.tables["equity_curve"].portfolio_nav.tolist(), [1.0])
        self.assertTrue(engine.tables["predictions"].empty)
        self.assertEqual(metrics["total_return"], 0.0)
        self.assertEqual(metrics["annualized_return"], 0.0)
        self.assertEqual(metrics["maximum_drawdown"], 0.0)
        self.assertIsNone(metrics["annualized_volatility"])
        self.assertIsNone(metrics["sharpe_ratio"])

    def test_real_financial_outputs_are_identical_in_all_async_modes(self):
        baseline = self.engine(prefetch=False, async_inference=False)
        baseline.run()
        for prefetch, asynchronous in ((False, True), (True, False), (True, True)):
            with self.subTest(prefetch=prefetch, async_inference=asynchronous):
                actual = self.engine(prefetch=prefetch, async_inference=asynchronous)
                actual.run()
                self.assertEqual(actual.metrics, baseline.metrics)
                self.assertEqual(actual.account, baseline.account)
                for name in RESULT_COLUMNS:
                    pd.testing.assert_frame_equal(actual.tables[name], baseline.tables[name])


if __name__ == "__main__":
    unittest.main()
