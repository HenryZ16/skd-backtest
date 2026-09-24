"""Public-contract checks; no financial algorithm is implemented yet."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import unittest

import pandas as pd

from skd_backtest import BacktestEngine, CostConfig, FeeScheduleEntry, OptimizerConfig


class SkeletonTest(unittest.TestCase):
    def test_walkthrough_contract_and_repeat_run(self):
        calls = []
        scores = pd.DataFrame([{"date": "2024-01-02", "code": "SH600000", "score": 1.0}])

        class Model:
            def predict(model, *, as_of_date, data):
                calls.append(as_of_date)
                self.assertEqual(set(data), {"MarketData", "Factor33_winsor", "Barra_factor"})
                self.assertEqual({name: table.shape for name, table in data.items()}, {
                    "Factor33_winsor": (0, 36), "Barra_factor": (0, 13), "MarketData": (0, 10),
                })
                return scores

        optimizer = OptimizerConfig(top_k=30, turnover_limit=0.2)
        costs = CostConfig(fee_schedule=(FeeScheduleEntry("2024-01-01", 0.0, 0.0),))
        # Deliberately absent path: the skeleton must perform no data/file IO.
        engine = BacktestEngine(
            data_dir="unused-skeleton-data", start_date="2024-01-02", end_date="2024-12-31",
            inference=Model().predict, initial_cash=123.0, rebalance_interval=10,
            holding_period=20, lookback=60, optimizer_config=optimizer, cost_config=costs,
            output_dir="unused-skeleton-results",
        )
        output = StringIO()
        with redirect_stdout(output):
            metrics = engine.run()

        self.assertEqual(calls, ["2024-01-02"])
        self.assertEqual(set(metrics), {
            "mean_rankic", "rankic_std", "rankic_ir", "positive_rankic_ratio",
            "total_return", "annualized_return", "annualized_excess_return",
            "annualized_volatility", "maximum_drawdown", "tracking_error",
            "information_ratio", "sharpe_ratio", "turnover", "transaction_cost", "failed_orders",
        })
        self.assertTrue(all(value is None for value in metrics.values()))
        self.assertIs(engine.metrics, metrics)
        self.assertIs(engine.optimizer.config, optimizer)
        self.assertIs(engine.cost_model.config, costs)
        self.assertEqual((engine.config.rebalance_interval, engine.config.holding_period, engine.config.lookback), (10, 20, 60))
        self.assertEqual(engine.data_provider.monthly_path("MarketData", 2024, 1),
                         Path("unused-skeleton-data/MarketData/2024/01/202401.parquet"))
        self.assertEqual(engine.account["cash"], 123.0)
        self.assertEqual(engine.account["total_shares"], {})
        self.assertEqual(set(engine.tables), {
            "predictions", "rankic", "target_weights", "orders", "trades", "positions", "equity_curve",
        })
        pd.testing.assert_frame_equal(engine.tables["predictions"][["date", "code", "score"]], scores)
        self.assertIsNone(engine.tables["predictions"].iloc[0]["future_return"])
        self.assertEqual(list(scores.columns), ["date", "code", "score"])
        self.assertTrue(all(table.empty for name, table in engine.tables.items() if name != "predictions"))

        trace = output.getvalue()
        stages = [
            "DataProvider.prepare", "DataProvider.as_of", "SubmissionRunner.predict",
            "PortfolioOptimizer.optimize", "CorporateActionEngine.apply", "DataProvider.open_market",
            "Broker.execute", "Broker.sell_orders", "CostModel.calculate", "Broker.buy_orders",
            "PortfolioAccounting.mark_to_market", "DataProvider.future_returns",
            "PredictionEvaluator.evaluate", "Metrics.calculate", "ResultWriter.write",
        ]
        positions = [trace.index(f"[{stage}]") for stage in stages]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("next trading day unresolved", trace)
        self.assertIn("no files written", trace)
        self.assertFalse(Path("unused-skeleton-results").exists())

        engine.account["cash"] = -1  # Run state must be initialized afresh.
        with redirect_stdout(StringIO()):
            second = engine.run()
        self.assertEqual(calls, ["2024-01-02", "2024-01-02"])
        self.assertEqual(engine.account["cash"], 123.0)
        self.assertEqual(second, metrics)
        self.assertEqual(len(engine.tables["predictions"]), 1)

    def test_inference_errors_propagate(self):
        def inference(*, as_of_date, data):
            raise RuntimeError("model failed")

        engine = BacktestEngine(
            data_dir="unused-skeleton-data", start_date="2024-01-02", end_date="2024-01-05",
            inference=inference,
        )
        with redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "model failed"):
            engine.run()
        self.assertIsNone(engine.metrics)


if __name__ == "__main__":
    unittest.main()
