"""Exercise the real daily loop with small monthly Parquet calendars."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from skd_backtest import BacktestEngine, CostConfig, FeeScheduleEntry, OptimizerConfig
from skd_backtest.schemas import RESULT_COLUMNS, SOURCE_COLUMNS


class SkeletonTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        # Repeated, unsorted dates with rows outside the selected range.
        for month, dates in {
            "201712": [20171229, 20171228, 20171229],
            "201801": [20180109, 20180105, 20180102, 20180108, 20180103, 20180104, 20180102],
        }.items():
            for name, columns in SOURCE_COLUMNS.items():
                folder = self.root / name / month[:4] / month[4:]
                folder.mkdir(parents=True)
                rows = []
                for day in dict.fromkeys(dates):
                    for code in ("SH600000", "SZ000001"):
                        row = dict.fromkeys(columns, 1.0)
                        row.update(日期=day, 代码=code, 名称=code)
                        rows.append(row)
                pd.DataFrame(rows).to_parquet(folder / f"{month}.parquet", index=False)
        self.dates = ["2018-01-02", "2018-01-03", "2018-01-04", "2018-01-05", "2018-01-08"]
        self.calls = []

    def inference(self, *, as_of_date, data):
        self.calls.append(as_of_date)
        self.assertEqual(set(data), set(SOURCE_COLUMNS))
        for name, table in data.items():
            self.assertEqual(tuple(table.columns), SOURCE_COLUMNS[name])
            self.assertFalse(table.empty)
            self.assertLessEqual(table["日期"].max(), int(as_of_date.replace("-", "")))
        return pd.DataFrame([{"date": as_of_date, "code": "SH600000", "score": 1.0}])

    def make_engine(self, **kwargs):
        config = dict(
            data_dir=self.root, start_date="2017-12-30", end_date="2018-01-08",
            inference=self.inference, rebalance_interval=2,
        )
        config.update(kwargs)
        return BacktestEngine(**config)

    def test_daily_calendar_order_and_repeat_run(self):
        optimizer = OptimizerConfig(top_k=30, turnover_limit=0.2)
        costs = CostConfig(fee_schedule=(FeeScheduleEntry("2018-01-01", 0.0, 0.0),))
        engine = self.make_engine(
            initial_cash=123.0, holding_period=20, lookback=60,
            optimizer_config=optimizer, cost_config=costs, output_dir=self.root / "result",
        )
        with (self.assertLogs("skd_backtest", level="DEBUG") as output,
              patch("pandas.read_parquet", wraps=pd.read_parquet) as read):
            metrics = engine.run()

        self.assertEqual(engine.trading_dates, self.dates)
        self.assertEqual(self.calls, ["2018-01-02", "2018-01-04"])
        self.assertEqual(read.call_count, 8)  # Two calendars and three datasets x two months.
        self.assertEqual(sum(call.kwargs["columns"] == ["日期"] for call in read.call_args_list), 2)
        self.assertEqual(engine.performance["trading_days"], 5)
        self.assertEqual(engine.performance["market_rows"], 10)
        self.assertEqual(engine.performance["data"]["data_files"], 6)
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

        trace = "\n".join(record.getMessage() for record in output.records)
        execution_dates = {"2018-01-03", "2018-01-05"}
        for date in self.dates:
            day_trace = trace.split(f"[BacktestEngine.day] {date}\n")[1].split("[BacktestEngine.day]")[0]
            stages = ["CorporateActionEngine.apply", "Broker.start_day", "PortfolioAccounting.mark_at_open"]
            if date in execution_dates:
                stages += ["Broker.execute", "Broker.sell_orders", "CostModel.calculate", "Broker.buy_orders"]
            else:
                self.assertNotIn("[Broker.execute]", day_trace)
            stages += ["PortfolioAccounting.mark_to_market"]
            if date in self.calls:
                stages += ["SubmissionRunner.predict", "PortfolioAccounting.current_weights", "PortfolioOptimizer.optimize"]
            offsets = [day_trace.index(f"[{stage}]") for stage in stages]
            self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(trace.count("[PortfolioAccounting.mark_at_open]"), len(self.dates))
        self.assertEqual(trace.count("[PortfolioAccounting.mark_to_market]"), len(self.dates))
        self.assertEqual(trace.count("[DataProvider.close_market]"), len(self.dates))
        self.assertLess(trace.rindex("[BacktestEngine.day]"), trace.index("[DataProvider.future_returns]"))
        self.assertEqual(trace.count("[Metrics.calculate]"), 1)
        self.assertEqual(trace.count("[ResultWriter.write]"), 1)
        self.assertFalse((self.root / "result").exists())

        engine.account["cash"] = -1
        with (patch("builtins.print", side_effect=AssertionError("framework must not print")),
              self.assertNoLogs("skd_backtest", level="WARNING")):
            second = engine.run()
        self.assertEqual(self.calls, ["2018-01-02", "2018-01-04"] * 2)
        self.assertEqual(engine.account["cash"], 123.0)
        self.assertEqual(second, metrics)
        self.assertEqual(len(engine.tables["predictions"]), 2)

    def test_daily_signals_next_open_and_audit_accumulation(self):
        engine = self.make_engine(rebalance_interval=1)

        def row(name, **values):
            return pd.DataFrame([values], columns=RESULT_COLUMNS[name])

        def optimize(**kwargs):
            date = kwargs["scores"]["date"].iloc[0]
            expected_equity = 20.0 + self.dates.index(date)
            self.assertEqual(engine.account["valuation_at"], (date, "close"))
            self.assertEqual(kwargs["current_weights"]["weight"].tolist(), [expected_equity / 100.0])
            return row("target_weights", code="SH600000", target_weight=1.0)

        def mark_at_open(*, date, account, market, price_mode):
            self.assertIs(account, engine.account)
            self.assertEqual(price_mode, "adjusted_return")
            self.assertEqual(set(market), {
                "date", "code", "adjusted_open", "is_suspended", "is_missing",
                "previous_close", "previous_close_date",
            })
            self.assertEqual(market["date"].unique().tolist(), [date])
            self.assertTrue((market["previous_close_date"].dropna() < int(date.replace("-", ""))).all())
            # Deliberately different open/close values detect stale or misordered state forwarding.
            account["portfolio_value"] = 10.0 + self.dates.index(date)
            account["valuation_at"] = (date, "open")

        def execute(**kwargs):
            date = kwargs["date"]
            account = kwargs["account"]
            self.assertIs(account, engine.account)
            self.assertEqual(account["valuation_at"], (date, "open"))
            self.assertEqual(account["portfolio_value"], 10.0 + self.dates.index(date))
            self.assertNotIn("adjusted_close", kwargs["market"])
            targets = kwargs["target_weights"]
            previous = self.dates[self.dates.index(date) - 1]
            self.assertEqual(targets["signal_date"].tolist(), [previous])
            self.assertEqual(targets["execution_date"].tolist(), [date])
            account["cash"] -= 1.0
            return row("orders", date=date), row("trades", date=date)

        def mark_to_market(*, date, account, market, price_mode):
            self.assertIs(account, engine.account)
            self.assertEqual(price_mode, "adjusted_return")
            self.assertEqual(market["date"].unique().tolist(), [date])
            self.assertIn("adjusted_close", market)
            self.assertIn("reference_close", market)
            self.assertNotIn("adjusted_open", market)
            self.assertEqual(account["cash"], engine.config.initial_cash - self.dates.index(date))
            account["portfolio_value"] = 20.0 + self.dates.index(date)
            account["valuation_at"] = (date, "close")
            return (row("positions", date=date),
                    row("equity_curve", date=date, cash=account["cash"], portfolio_value=account["portfolio_value"]))

        def current_weights(account):
            self.assertIs(account, engine.account)
            self.assertEqual(account["valuation_at"][1], "close")
            return pd.DataFrame({"code": ["SH600000"], "weight": [account["portfolio_value"] / 100.0]})

        with (patch("builtins.print", side_effect=AssertionError("framework must not print")),
              patch.object(engine.optimizer, "optimize", side_effect=optimize),
              patch.object(engine.broker, "execute", side_effect=execute),
              patch.object(engine.accounting, "mark_at_open", side_effect=mark_at_open),
              patch.object(engine.accounting, "current_weights", side_effect=current_weights),
              patch.object(engine.accounting, "mark_to_market", side_effect=mark_to_market)):
            engine.run()

        self.assertEqual(self.calls, self.dates[:-1])
        for name in ("orders", "trades"):
            self.assertEqual(engine.tables[name]["date"].tolist(), self.dates[1:])
        for name in ("positions", "equity_curve"):
            self.assertEqual(engine.tables[name]["date"].tolist(), self.dates)
        self.assertEqual(engine.tables["equity_curve"]["portfolio_value"].tolist(), [20., 21., 22., 23., 24.])
        self.assertEqual(engine.tables["target_weights"]["execution_date"].tolist(), self.dates[1:])

    def test_single_day_and_no_trading_days(self):
        for start, end, expected in [
            ("2018-01-02", "2018-01-02", ["2018-01-02"]),
            ("2017-12-30", "2018-01-01", []),
        ]:
            with self.subTest(start=start, end=end):
                engine = self.make_engine(start_date=start, end_date=end)
                with (redirect_stdout(StringIO()),
                      patch.object(engine.accounting, "mark_at_open", wraps=engine.accounting.mark_at_open) as open_mark,
                      patch.object(engine.accounting, "mark_to_market", wraps=engine.accounting.mark_to_market) as close,
                      patch.object(engine.broker, "execute", wraps=engine.broker.execute) as execute):
                    engine.run()
                self.assertEqual(engine.trading_dates, expected)
                self.assertEqual(open_mark.call_count, len(expected))
                self.assertEqual(close.call_count, len(expected))
                execute.assert_not_called()
                self.assertEqual(self.calls, [])
                self.assertEqual(set(engine.tables), set(RESULT_COLUMNS))

    def test_accounting_failure_stops_decisions_and_cleans_up_reader(self):
        for method in ("mark_at_open", "mark_to_market"):
            engine = self.make_engine()
            with (self.subTest(method=method),
                  patch.object(engine.accounting, method, side_effect=RuntimeError("valuation failed")),
                  patch.object(engine.optimizer, "optimize") as optimize,
                  self.assertRaisesRegex(RuntimeError, "valuation failed")):
                engine.run()
            optimize.assert_not_called()
            self.assertEqual(self.calls, [])
            self.assertIsNone(engine.metrics)
            self.assertIsNone(engine.data_provider._executor)
            self.assertFalse(engine.data_provider._tables)

    def test_inference_errors_propagate(self):
        def inference(*, as_of_date, data):
            raise RuntimeError("model failed")

        engine = self.make_engine(inference=inference)
        with redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "model failed"):
            engine.run()
        self.assertIsNone(engine.metrics)


if __name__ == "__main__":
    unittest.main()
