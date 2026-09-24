"""Exercise the real daily loop with small monthly Parquet calendars."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from skd_backtest import BacktestEngine, CostConfig, FeeScheduleEntry, OptimizerConfig
from skd_backtest.schemas import RESULT_COLUMNS


class SkeletonTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        # Repeated, unsorted dates with rows outside the selected range.
        for month, dates in {
            "202312": [20231229, 20231228, 20231229],
            "202401": [20240109, 20240105, 20240102, 20240108, 20240103, 20240104, 20240102],
        }.items():
            folder = self.root / "MarketData" / month[:4] / month[4:]
            folder.mkdir(parents=True)
            pd.DataFrame({"日期": dates, "close": 1.0}).to_parquet(folder / f"{month}.parquet", index=False)
        self.dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
        self.calls = []

    def inference(self, *, as_of_date, data):
        self.calls.append(as_of_date)
        self.assertEqual({name: table.shape for name, table in data.items()}, {
            "Factor33_winsor": (0, 36), "Barra_factor": (0, 13), "MarketData": (0, 10),
        })
        return pd.DataFrame([{"date": as_of_date, "code": "SH600000", "score": 1.0}])

    def make_engine(self, **kwargs):
        config = dict(
            data_dir=self.root, start_date="2023-12-30", end_date="2024-01-08",
            inference=self.inference, rebalance_interval=2,
        )
        config.update(kwargs)
        return BacktestEngine(**config)

    def test_daily_calendar_order_and_repeat_run(self):
        optimizer = OptimizerConfig(top_k=30, turnover_limit=0.2)
        costs = CostConfig(fee_schedule=(FeeScheduleEntry("2024-01-01", 0.0, 0.0),))
        engine = self.make_engine(
            initial_cash=123.0, holding_period=20, lookback=60,
            optimizer_config=optimizer, cost_config=costs, output_dir=self.root / "result",
        )
        output = StringIO()
        with redirect_stdout(output), patch("pandas.read_parquet", wraps=pd.read_parquet) as read:
            metrics = engine.run()

        self.assertEqual(engine.trading_dates, self.dates)
        self.assertEqual(self.calls, ["2024-01-02", "2024-01-04"])
        self.assertEqual(read.call_count, 2)
        self.assertTrue(all(call.kwargs["columns"] == ["日期"] for call in read.call_args_list))
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
        self.assertEqual((engine.config.holding_period, engine.config.lookback), (20, 60))
        self.assertEqual(engine.account["cash"], 123.0)
        self.assertEqual(engine.account["total_shares"], {})
        self.assertEqual(set(engine.tables), set(RESULT_COLUMNS))
        self.assertEqual(engine.tables["predictions"]["date"].tolist(), self.calls)
        self.assertTrue(engine.tables["predictions"]["future_return"].isna().all())

        trace = output.getvalue()
        execution_dates = {"2024-01-03", "2024-01-05"}
        for date in self.dates:
            day_trace = trace.split(f"[BacktestEngine.day] {date}\n")[1].split("[BacktestEngine.day]")[0]
            stages = ["CorporateActionEngine.apply", "DataProvider.open_market", "Broker.start_day"]
            if date in execution_dates:
                stages += ["Broker.execute", "Broker.sell_orders", "CostModel.calculate", "Broker.buy_orders"]
            else:
                self.assertNotIn("[Broker.execute]", day_trace)
            stages += ["DataProvider.close_market", "PortfolioAccounting.mark_to_market"]
            if date in self.calls:
                stages += ["DataProvider.as_of", "SubmissionRunner.predict", "PortfolioOptimizer.optimize"]
            offsets = [day_trace.index(f"[{stage}]") for stage in stages]
            self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(trace.count("[PortfolioAccounting.mark_to_market]"), len(self.dates))
        self.assertLess(trace.rindex("[PortfolioAccounting.mark_to_market]"), trace.index("[DataProvider.future_returns]"))
        self.assertEqual(trace.count("[Metrics.calculate]"), 1)
        self.assertEqual(trace.count("[ResultWriter.write]"), 1)
        self.assertFalse((self.root / "result").exists())

        engine.account["cash"] = -1
        with redirect_stdout(StringIO()):
            second = engine.run()
        self.assertEqual(self.calls, ["2024-01-02", "2024-01-04"] * 2)
        self.assertEqual(engine.account["cash"], 123.0)
        self.assertEqual(second, metrics)
        self.assertEqual(len(engine.tables["predictions"]), 2)

    def test_daily_signals_next_open_and_audit_accumulation(self):
        engine = self.make_engine(rebalance_interval=1)

        def row(name, **values):
            return pd.DataFrame([values], columns=RESULT_COLUMNS[name])

        def optimize(**kwargs):
            return row("target_weights", code="SH600000", target_weight=1.0)

        def execute(**kwargs):
            date = kwargs["date"]
            targets = kwargs["target_weights"]
            previous = self.dates[self.dates.index(date) - 1]
            self.assertEqual(targets["signal_date"].tolist(), [previous])
            self.assertEqual(targets["execution_date"].tolist(), [date])
            return row("orders", date=date), row("trades", date=date)

        def close(**kwargs):
            return row("positions", date=kwargs["date"]), row("equity_curve", date=kwargs["date"])

        with (redirect_stdout(StringIO()),
              patch.object(engine.optimizer, "optimize", side_effect=optimize),
              patch.object(engine.broker, "execute", side_effect=execute),
              patch.object(engine.accounting, "mark_to_market", side_effect=close)):
            engine.run()

        self.assertEqual(self.calls, self.dates[:-1])
        for name in ("orders", "trades"):
            self.assertEqual(engine.tables[name]["date"].tolist(), self.dates[1:])
        for name in ("positions", "equity_curve"):
            self.assertEqual(engine.tables[name]["date"].tolist(), self.dates)
        self.assertEqual(engine.tables["target_weights"]["execution_date"].tolist(), self.dates[1:])

    def test_single_day_and_no_trading_days(self):
        for start, end, expected in [
            ("2024-01-02", "2024-01-02", ["2024-01-02"]),
            ("2023-12-30", "2024-01-01", []),
        ]:
            with self.subTest(start=start, end=end):
                engine = self.make_engine(start_date=start, end_date=end)
                with (redirect_stdout(StringIO()),
                      patch.object(engine.accounting, "mark_to_market", wraps=engine.accounting.mark_to_market) as close,
                      patch.object(engine.broker, "execute", wraps=engine.broker.execute) as execute):
                    engine.run()
                self.assertEqual(engine.trading_dates, expected)
                self.assertEqual([call.kwargs["date"] for call in close.call_args_list], expected)
                execute.assert_not_called()
                self.assertEqual(self.calls, [])
                self.assertEqual(set(engine.tables), set(RESULT_COLUMNS))

    def test_inference_errors_propagate(self):
        def inference(*, as_of_date, data):
            raise RuntimeError("model failed")

        engine = self.make_engine(inference=inference)
        with redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "model failed"):
            engine.run()
        self.assertIsNone(engine.metrics)


if __name__ == "__main__":
    unittest.main()
