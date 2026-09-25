"""Protocol checks use synthetic packets, not financial implementations."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

import skd_backtest.runtime_cache as cache_module

import pandas as pd
from pandas.testing import assert_frame_equal

from skd_backtest import BacktestEngine
from skd_backtest.contracts import (
    ComponentRole as Role, CostQuote, CostRequest, Dataset, ExecutionResult,
    MarketContext, OpenSnapshot, CloseSnapshot, OutputReceipt, Phase, PredictionResult,
    RunCalendar, RunContext, TargetPlan, Topic,
)
from skd_backtest.reference_data import ReferenceDataProvider
from skd_backtest.runtime_cache import RuntimeCache
from skd_backtest.schemas import (
    LABEL_COLUMNS, METRIC_NAMES, RESULT_COLUMNS, VALUE_COLUMNS, WEIGHT_COLUMNS, empty_result,
)
from skd_backtest.submission_runner import SubmissionRunner


class RuntimeCacheTest(unittest.TestCase):
    dates = ("2018-01-02", "2018-01-03", "2018-01-04")

    def setUp(self):
        self.engine = BacktestEngine(data_dir=Path("."), start_date=self.dates[0], end_date=self.dates[-1],
                                     inference=lambda **kw: None, initial_cash=100.0, rebalance_interval=1)
        self.cache = RuntimeCache(context=RunContext(self.engine.config, self.engine.optimizer_config, self.engine.cost_config))
        self.addCleanup(self.cache.close)
        self.views = {role: self.cache.for_component(role) for role in Role}
        self.root = self.views[Role.ENGINE]
        self.state = self.root.read(Topic.ACCOUNT_INITIAL).account
        self.root.publish(Topic.RUN_CALENDAR, None, RunCalendar.from_dates(self.dates, 1))

    def settle_day(self, day):
        self.cache.advance(date=day, phase=Phase.SETTLEMENT)
        self.views[Role.BROKER].publish(Topic.ACCOUNT_SETTLED, day, self.state)
        self.cache.advance(date=day, phase=Phase.OPEN_VALUE)
        self.views[Role.ACCOUNTING].publish(Topic.ACCOUNT_OPEN, day,
            OpenSnapshot(day, self.state, pd.DataFrame(columns=VALUE_COLUMNS), 0.0, 100.0))
        self.cache.advance(date=day, phase=Phase.EXECUTION)

    def close_day(self, day, *, finish=True):
        signal = self.dates[self.dates.index(day) - 1] if day != self.dates[0] else None
        self.views[Role.BROKER].publish(Topic.EXECUTION_DAY, day,
            ExecutionResult(day, self.state, empty_result("orders"), empty_result("trades"), 0.0, 0.0, signal))
        self.cache.advance(date=day, phase=Phase.CLOSE_VALUE)
        self.views[Role.REFERENCE_DATA].publish(Topic.REFERENCE_BENCHMARK, day, Dataset("unavailable", None, "disabled"))
        equity = pd.DataFrame([{"date": day, "cash": 100.0, "market_value": 0.0, "portfolio_value": 100.0,
                               "portfolio_nav": 1.0, "portfolio_return": 0.0, "turnover": 0.0, "transaction_cost": 0.0}],
                              columns=RESULT_COLUMNS["equity_curve"])
        self.views[Role.ACCOUNTING].publish(Topic.ACCOUNT_CLOSE, day,
            CloseSnapshot(day, self.state, empty_result("positions"), equity, pd.DataFrame(columns=WEIGHT_COLUMNS)))
        if day != self.dates[-1]:
            self.cache.advance(date=day, phase=Phase.SIGNAL)
            universe = pd.DataFrame({"code": ["SH600000", "SZ000001"]})
            self.root.publish(Topic.MARKET_CONTEXT, day, MarketContext(
                day, universe, universe.assign(date=day, beta=1.0, notes=[{"tags": [1]}, {"tags": [2]}])))
            ReferenceDataProvider(self.engine.config).prepare_signal(date=day, cache=self.views[Role.REFERENCE_DATA])
            runner = SubmissionRunner(lambda **kw: universe.assign(date=day, score=[1.0, 2.0]))
            runner.predict(as_of_date=day, data={}, cache=self.views[Role.RUNNER])
            execution = self.dates[self.dates.index(day) + 1]
            self.views[Role.OPTIMIZER].publish(Topic.SIGNAL_TARGETS, execution,
                                               TargetPlan(day, execution, empty_result("target_weights")))
        if finish:
            self.cache.finish_day(date=day)

    def test_stages_permissions_dates_and_borrowed_references(self):
        day = self.dates[0]
        with self.assertRaises(RuntimeError):
            self.cache.advance(date=day, phase=Phase.EXECUTION)
        self.cache.advance(date=day, phase=Phase.SETTLEMENT)
        broker, accounting = self.views[Role.BROKER], self.views[Role.ACCOUNTING]
        with self.assertRaises(KeyError):
            accounting.read(Topic.ACCOUNT_SETTLED, day)
        with self.assertRaises(PermissionError):
            broker.read(Topic.EVALUATION_LABELS)
        with self.assertRaises(PermissionError):
            accounting.publish(Topic.ACCOUNT_SETTLED, day, self.state)
        with self.assertRaises(ValueError):
            broker.publish(Topic.ACCOUNT_SETTLED, self.dates[1], self.state)
        with self.assertRaises(RuntimeError):
            self.cache.advance(date=day, phase=Phase.OPEN_VALUE)
        broker.publish(Topic.ACCOUNT_SETTLED, day, self.state)
        borrowed = accounting.read(Topic.ACCOUNT_SETTLED, day)
        self.assertIs(borrowed, self.state)
        positions = borrowed.positions.copy()
        positions.loc[0] = ["SH600000", 50.0, 10.0, day]
        next_state = replace(borrowed, positions=positions)
        self.assertIs(next_state.locked_lots, self.state.locked_lots)
        self.assertTrue(borrowed.positions.empty)
        with self.assertRaises(RuntimeError):
            broker.publish(Topic.ACCOUNT_SETTLED, day, self.state)
        with self.assertRaises(PermissionError):
            broker.read(Topic.ACCOUNT_CLOSE, self.dates[1])
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(RuntimeError):
                pool.submit(broker.read, Topic.RUN_CONTEXT).result()

    def test_schemas_are_checked_once_and_empty_history_is_not_retained(self):
        first, second, third = self.dates
        with patch.object(cache_module, "_frame", wraps=cache_module._frame) as checks:
            self.settle_day(first)
            self.close_day(first)
            first_day_checks = checks.call_count
            self.assertGreater(first_day_checks, 0)
            self.settle_day(second)
            self.close_day(second)
            self.settle_day(third)
            self.close_day(third)
            self.assertEqual(checks.call_count, first_day_checks)
        self.assertFalse(self.cache._audit["positions"])

    def test_invalid_initial_schema_can_be_corrected_before_publication(self):
        day = self.dates[0]
        self.cache.advance(date=day, phase=Phase.SETTLEMENT)
        broker = self.views[Role.BROKER]
        with self.assertRaises(ValueError):
            broker.publish(Topic.ACCOUNT_SETTLED, day, replace(self.state, positions=pd.DataFrame()))
        self.assertFalse(self.root.contains(Topic.ACCOUNT_SETTLED, day))
        broker.publish(Topic.ACCOUNT_SETTLED, day, self.state)

    def test_settlement_reads_initial_and_then_previous_close(self):
        first, second = self.dates[:2]
        self.cache.advance(date=first, phase=Phase.SETTLEMENT)
        self.engine.broker.start_day(date=first, cache=self.views[Role.BROKER])
        self.assertIs(self.root.read(Topic.ACCOUNT_SETTLED, first), self.state)
        self.cache.advance(date=first, phase=Phase.OPEN_VALUE)
        self.views[Role.ACCOUNTING].publish(Topic.ACCOUNT_OPEN, first,
            OpenSnapshot(first, self.state, pd.DataFrame(columns=VALUE_COLUMNS), 0.0, 100.0))
        self.cache.advance(date=first, phase=Phase.EXECUTION)
        self.state = replace(self.state, cash=90.0)
        self.close_day(first)
        self.cache.advance(date=second, phase=Phase.SETTLEMENT)
        self.engine.broker.start_day(date=second, cache=self.views[Role.BROKER])
        self.assertIs(self.root.read(Topic.ACCOUNT_SETTLED, second), self.state)
        self.assertEqual(self.root.read(Topic.ACCOUNT_SETTLED, second).cash, 90.0)

    def test_quote_identity_revisions_and_release(self):
        day = self.dates[0]
        self.settle_day(day)
        broker, cost = self.views[Role.BROKER], self.views[Role.COST_MODEL]
        request = CostRequest(1, "order-1", day, "BUY", "adjusted_return", None, None, 10.0)
        broker.publish(Topic.COST_REQUEST, (day, 1), request)
        with self.assertRaises(RuntimeError):
            broker.publish(Topic.COST_REQUEST, (day, 2), replace(request, request_id=2))
        with self.assertRaises(RuntimeError):
            broker.finish_quote(date=day, request_id=1)
        quote = CostQuote(1, "order-1", day, "BUY", "adjusted_return", None,
                          10.0, 10.0, 1.0, 0.0, 0.0, 1.0, -11.0)
        with self.assertRaises(ValueError):
            cost.publish(Topic.COST_RESULT, (day, 1), replace(quote, side="SELL"))
        self.assertFalse(self.root.contains(Topic.COST_RESULT, (day, 1)))
        cost.publish(Topic.COST_RESULT, (day, 1), quote)
        self.assertEqual(broker.read(Topic.COST_RESULT, (day, 1)).cash_delta, -11.0)
        with self.assertRaises(RuntimeError):
            self.close_day(day)
        broker.finish_quote(date=day, request_id=1)
        with self.assertRaises(RuntimeError):
            broker.publish(Topic.COST_REQUEST, (day, 1), request)
        broker.publish(Topic.COST_REQUEST, (day, 2), replace(request, request_id=2, position_value=5.0))
        cost.publish(Topic.COST_RESULT, (day, 2), replace(quote, request_id=2, position_value=5.0, trade_value=5.0, cash_delta=-6.0))
        broker.finish_quote(date=day, request_id=2)
        broker.publish(Topic.COST_REQUEST, (day, 3), replace(request, request_id=3))
        self.engine.cost_model.calculate(date=day, request_id=3, cache=cost)
        quote_result = broker.read(Topic.COST_RESULT, (day, 3))
        self.assertEqual((quote_result.execution_price, quote_result.cash_delta, quote_result.total_cost),
                         (None, -10.0, 0.0))
        broker.finish_quote(date=day, request_id=3)
        self.close_day(day)
        self.assertFalse(self.root.contains(Topic.COST_REQUEST, (day, 2)))

    def test_retention_history_evaluation_and_closed_views(self):
        first, second, third = self.dates
        self.settle_day(first)
        self.close_day(first)
        self.assertTrue(self.root.contains(Topic.SIGNAL_TARGETS, second))
        self.assertFalse(self.root.contains(Topic.ACCOUNT_OPEN, first))
        self.settle_day(second)
        with self.assertRaises(PermissionError):
            self.views[Role.BROKER].read(Topic.SIGNAL_TARGETS, third)
        self.assertEqual(self.views[Role.BROKER].read(Topic.SIGNAL_TARGETS, second).signal_date, first)
        self.close_day(second)
        self.assertFalse(self.root.contains(Topic.ACCOUNT_CLOSE, first))
        self.assertFalse(self.root.contains(Topic.SIGNAL_TARGETS, second))
        self.settle_day(third)
        self.close_day(third)
        with self.assertRaises(PermissionError):
            self.views[Role.OPTIMIZER].history(Topic.SIGNAL_SCORES)
        self.cache.advance(date=None, phase=Phase.EVALUATION)
        labels = self.views[Role.LABEL_PROVIDER]
        scores = labels.history(Topic.SIGNAL_SCORES)
        self.assertEqual(len(scores), 4)
        self.assertIs(scores, labels.history(Topic.SIGNAL_SCORES))
        assert_frame_equal(labels.history(Topic.SIGNAL_SCORES, date=first),
                           scores.loc[scores.date == first])
        with self.assertRaises(PermissionError):
            labels.read(Topic.EVALUATION_LABELS)
        labels.publish(Topic.EVALUATION_LABELS, None, pd.DataFrame(columns=LABEL_COLUMNS))
        evaluator = self.views[Role.EVALUATOR]
        self.assertTrue(evaluator.read(Topic.EVALUATION_LABELS).empty)
        with self.assertRaises(RuntimeError):
            self.root.result_tables()
        predictions = evaluator.history(Topic.SIGNAL_SCORES).assign(future_return=None)
        evaluator.publish(Topic.EVALUATION_PREDICTION, None, PredictionResult(predictions, empty_result("rankic")))
        self.cache.advance(date=None, phase=Phase.METRICS)
        tables = self.views[Role.METRICS].result_tables()
        self.assertEqual(tables["equity_curve"].date.tolist(), list(self.dates))
        self.assertIs(tables, self.root.result_tables())
        self.assertIs(tables["predictions"], predictions)
        self.views[Role.METRICS].publish(Topic.EVALUATION_METRICS, None, dict.fromkeys(METRIC_NAMES))
        self.cache.advance(date=None, phase=Phase.OUTPUT)
        self.views[Role.WRITER].publish(Topic.OUTPUT_RECEIPT, None, OutputReceipt("disabled", None, {}))
        self.cache.close()
        self.assertEqual(tables["equity_curve"].cash.tolist(), [100.0] * 3)
        with self.assertRaises(RuntimeError):
            evaluator.history(Topic.SIGNAL_SCORES)

    def test_borrowed_nested_objects_and_bad_calendar_are_atomic(self):
        cache = RuntimeCache(context=RunContext(self.engine.config, self.engine.optimizer_config, self.engine.cost_config))
        self.addCleanup(cache.close)
        root = cache.for_component(Role.ENGINE)
        wrong = RunCalendar(self.dates, pd.DataFrame([(self.dates[0], self.dates[2])],
                                                     columns=["signal_date", "execution_date"]))
        with self.assertRaises(ValueError):
            root.publish(Topic.RUN_CALENDAR, None, wrong)
        self.assertFalse(root.contains(Topic.RUN_CALENDAR))
        # Exercise a table containing mutable object cells through a permitted packet.
        self.settle_day(self.dates[0])
        self.close_day(self.dates[0], finish=False)
        source = self.root.read(Topic.MARKET_CONTEXT, self.dates[0])
        # Borrowing retains the packet and all nested buffers; consumers do not mutate them.
        assert_frame_equal(source.universe, pd.DataFrame({"code": ["SH600000", "SZ000001"]}))
        fresh = self.root.read(Topic.MARKET_CONTEXT, self.dates[0])
        self.assertIs(source, fresh)
        self.assertIs(source.barra_exposures.at[0, "notes"], fresh.barra_exposures.at[0, "notes"])

    def test_raw_cost_packets_keep_share_and_amount_modes_distinct(self):
        self.cache.close()
        config = replace(self.engine.config, price_mode="raw_price")
        self.cache = RuntimeCache(context=RunContext(config, self.engine.optimizer_config, self.engine.cost_config))
        self.addCleanup(self.cache.close)
        self.views = {role: self.cache.for_component(role) for role in Role}
        self.root = self.views[Role.ENGINE]
        self.state = self.root.read(Topic.ACCOUNT_INITIAL).account
        self.root.publish(Topic.RUN_CALENDAR, None, RunCalendar.from_dates(self.dates, 1))
        day = self.dates[0]
        self.settle_day(day)
        broker, cost = self.views[Role.BROKER], self.views[Role.COST_MODEL]
        request = CostRequest(1, "raw-order", day, "SELL", "raw_price", 10.0, 100, None)
        for invalid in (replace(request, shares=0), replace(request, shares=1.5),
                        replace(request, position_value=1000.0)):
            with self.assertRaises(ValueError):
                broker.publish(Topic.COST_REQUEST, (day, 1), invalid)
        self.assertFalse(self.root.contains(Topic.COST_REQUEST, (day, 1)))
        broker.publish(Topic.COST_REQUEST, (day, 1), request)
        quote = CostQuote(1, "raw-order", day, "SELL", "raw_price",
                          9.9, 1000.0, 990.0, 5.0, 1.0, 0.0, 6.0, 984.0)
        with self.assertRaises(ValueError):
            cost.publish(Topic.COST_RESULT, (day, 1), replace(quote, execution_price=None))
        cost.publish(Topic.COST_RESULT, (day, 1), quote)
        self.assertEqual(broker.read(Topic.COST_RESULT, (day, 1)).execution_price, 9.9)
        broker.finish_quote(date=day, request_id=1)
        broker.publish(Topic.COST_REQUEST, (day, 2), replace(request, request_id=2))
        self.engine.cost_model.calculate(date=day, request_id=2, cache=cost)
        quote_result = broker.read(Topic.COST_RESULT, (day, 2))
        self.assertEqual((quote_result.execution_price, quote_result.cash_delta, quote_result.total_cost),
                         (10.0, 1000.0, 0.0))
        broker.finish_quote(date=day, request_id=2)

    def test_logs_failure_isolation_and_dataset_states(self):
        for kwargs in (
            dict(status="available", data=None),
            dict(status="unavailable", data=pd.DataFrame(), reason="missing"),
            dict(status="unavailable", data=None),
        ):
            with self.assertRaises(ValueError):
                Dataset(**kwargs)
        details = {"values": [1]}
        self.views[Role.BROKER].log(level="INFO", message="sample", details=details)
        details["values"].append(2)
        writer = self.views[Role.WRITER]
        records = writer.log_records(after_seq=0)
        self.assertEqual(records[0].details, {"values": [1]})
        self.assertIs(writer.log_records(after_seq=0)[0], records[0])
        with self.assertRaises(PermissionError):
            self.views[Role.RUNNER].log_records(after_seq=0)
        self.cache.advance(date=None, phase=Phase.FAILED)
        self.views[Role.BROKER].log(level="ERROR", message="failure")
        self.assertEqual(writer.log_records(after_seq=1)[0].phase, Phase.FAILED)
        with self.assertRaises(RuntimeError):
            self.cache.advance(date=self.dates[0], phase=Phase.SETTLEMENT)


if __name__ == "__main__":
    unittest.main()
