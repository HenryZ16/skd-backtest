"""Verify the engine's cache wiring using explicit financial component doubles."""

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, current_thread, get_ident, enumerate as threads
import json
import unittest
from unittest.mock import patch

import pandas as pd

from skd_backtest import BacktestEngine
from skd_backtest.schemas import METRIC_NAMES, RESULT_COLUMNS, SOURCE_COLUMNS
from protocol_support import protocol_components


class SkeletonTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for month, dates in {
            "201712": [20171229, 20171228],
            "201801": [20180109, 20180105, 20180102, 20180108, 20180103, 20180104],
        }.items():
            for name, columns in SOURCE_COLUMNS.items():
                folder = self.root / name / month[:4] / month[4:]
                folder.mkdir(parents=True)
                rows = []
                for day in dates:
                    for code in ("SH600000", "SZ000001"):
                        row = dict.fromkeys(columns, 1.0)
                        row.update(日期=day, 代码=code, 名称=code)
                        rows.append(row)
                pd.DataFrame(rows).to_parquet(folder / f"{month}.parquet", index=False)
        self.dates = ["2018-01-02", "2018-01-03", "2018-01-04", "2018-01-05", "2018-01-08"]
        self.calls = []

    def inference(self, *, as_of_date, data):
        self.calls.append(as_of_date)
        for frame in data.values():
            self.assertLessEqual(frame["日期"].max(), int(as_of_date.replace("-", "")))
        return pd.DataFrame({"date": as_of_date, "code": ["SH600000", "SZ000001"], "score": [1.0, 2.0]})

    def make_engine(self, **kwargs):
        config = dict(data_dir=self.root, start_date="2017-12-30", end_date="2018-01-08",
                      inference=self.inference, rebalance_interval=2)
        config.update(kwargs)
        return BacktestEngine(**config)

    def test_daily_calendar_cache_wiring_and_repeat_run(self):
        engine = self.make_engine(initial_cash=123.0)
        with protocol_components(engine) as trace, patch("pandas.read_parquet", wraps=pd.read_parquet) as read:
            metrics = engine.run()
            self.assertEqual(read.call_count, 8)  # prepare followed by playback does not repeat the calendar.
            self.assertEqual(engine.trading_dates, self.dates)
            self.assertEqual(self.calls, ["2018-01-02", "2018-01-04"])
            self.assertEqual(set(metrics), set(METRIC_NAMES))
            self.assertEqual(set(engine.tables), set(RESULT_COLUMNS))
            self.assertEqual(engine.tables["equity_curve"].date.tolist(), self.dates)
            self.assertEqual(engine.tables["predictions"].date.tolist(),
                             ["2018-01-02"] * 2 + ["2018-01-04"] * 2)
            self.assertEqual([item for item in trace if item[0] == "target"],
                             [("target", "2018-01-02", "2018-01-03"), ("target", "2018-01-04", "2018-01-05")])
            for date in self.dates:
                indices = [trace.index((name, date)) for name in ("settle", "open", "execute", "close")]
                self.assertEqual(indices, sorted(indices))
            self.assertGreater(trace.index(("labels",)), trace.index(("close", self.dates[-1])))
            first = engine.tables["predictions"].copy()
            engine.account["cash"] = -1
            engine.run()
            self.assertEqual(engine.account["cash"], 123.0)
            pd.testing.assert_frame_equal(engine.tables["predictions"], first)
        self.assertIsNone(engine.data_provider._executor)

    def test_default_components_keep_suspended_assets_untraded(self):
        for prefetch, output in ((False, None), (True, self.root / "component-output")):
            with self.subTest(prefetch=prefetch):
                self.calls.clear()
                engine = self.make_engine(initial_cash=123.0, prefetch=prefetch, output_dir=output)
                metrics = engine.run()
                self.assertEqual(set(metrics), set(METRIC_NAMES))
                self.assertEqual(metrics["total_return"], 0.0)
                self.assertEqual(metrics["transaction_cost"], 0.0)
                self.assertEqual(metrics["failed_orders"], 4)
                self.assertEqual(engine.trading_dates, self.dates)
                self.assertEqual(self.calls, ["2018-01-02", "2018-01-04"])
                self.assertEqual(engine.account["cash"], 123.0)
                self.assertEqual(engine.account["portfolio_value"], 123.0)
                self.assertEqual(len(engine.tables["predictions"]), 4)
                self.assertTrue(engine.tables["predictions"].future_return.isna().all())
                self.assertEqual(len(engine.tables["rankic"]), 2)
                self.assertTrue(engine.tables["rankic"].rankic.isna().all())
                self.assertEqual(len(engine.tables["target_weights"]), 4)
                self.assertEqual(len(engine.tables["orders"]), 4)
                self.assertTrue(engine.tables["orders"].status.eq("REJECTED").all())
                self.assertTrue(engine.tables["orders"].reject_reason.eq("SUSPENDED").all())
                for name in ("trades", "positions"):
                    self.assertTrue(engine.tables[name].empty, name)
                self.assertEqual(engine.tables["equity_curve"].date.tolist(), self.dates)
                self.assertTrue(engine.tables["equity_curve"].portfolio_nav.eq(1.0).all())
                self.assertTrue(engine.tables["equity_curve"].portfolio_return.eq(0.0).all())
                self.assertGreater(engine.performance["elapsed_seconds"], 0)
                self.assertIsNone(engine.data_provider._executor)
                if output is None:
                    self.assertEqual(engine.run(), metrics)
                else:
                    records = [json.loads(line) for line in (output / "run.log").read_text(encoding="utf-8").splitlines()]
                    self.assertFalse(any("STUB" in record["message"] for record in records))
                    self.assertEqual(records[-1]["message"], "run completed")
                    self.assertEqual({path.name for path in output.iterdir()},
                                     {"run.log", "metrics.json", *(name + ".csv" for name in RESULT_COLUMNS)})

    def test_sync_and_async_inference_match_with_both_read_modes(self):
        for prefetch in (False, True):
            with self.subTest(prefetch=prefetch):
                baseline = self.make_engine(prefetch=prefetch, async_inference=False, rebalance_interval=1)
                baseline.run()
                actual = self.make_engine(prefetch=prefetch, async_inference=True, rebalance_interval=1)
                actual.run()
                self.assertEqual(actual.metrics, baseline.metrics)
                self.assertEqual(actual.account, baseline.account)
                self.assertEqual(actual.trading_dates, baseline.trading_dates)
                self.assertEqual(actual.performance["inference_calls"], baseline.performance["inference_calls"])
                self.assertEqual(baseline.performance["inference_wait_seconds"], 0.0)
                for name in RESULT_COLUMNS:
                    pd.testing.assert_frame_equal(actual.tables[name], baseline.tables[name])
                self.assertFalse(any(t.name.startswith(("skd-infer", "skd-data")) for t in threads()))

    def test_inference_overlaps_accounting_and_next_rebalance_with_bounded_inputs(self):
        first_started, first_opened = Event(), Event()
        next_started, execution_started = Event(), Event()
        owner = get_ident()
        model_threads, prepared = [], []
        engine = self.make_engine()
        first, execution, second = self.dates[:3]
        original_open = engine.accounting.mark_at_open
        original_optimize = engine.optimizer.optimize
        original_execute = engine.broker.execute
        original_as_of = engine.data_provider.as_of

        def inference(*, as_of_date, data):
            self.assertNotEqual(get_ident(), owner)
            model_threads.append(current_thread().name)
            if as_of_date == first:
                first_started.set()
                self.assertTrue(first_opened.wait(5), "current-day accounting could not overlap prediction")
            else:
                next_started.set()
                self.assertTrue(execution_started.wait(5), "next prediction blocked current execution")
            return self.inference(as_of_date=as_of_date, data=data)

        def prepare(date):
            prepared.append(date)
            return original_as_of(date)

        def open_mark(*, date, market, cache):
            self.assertEqual(get_ident(), owner)
            if date == first:
                self.assertTrue(first_started.wait(5))
                # Only current/next signal windows were prepared before the first account day.
                self.assertEqual(prepared, [first, second])
                first_opened.set()
            original_open(date=date, market=market, cache=cache)

        def optimize(*, signal_date, cache):
            self.assertEqual(get_ident(), owner)
            if signal_date == first:
                self.assertTrue(next_started.wait(5), "next signal was not started ahead of its account day")
                self.assertFalse(execution_started.is_set())
            original_optimize(signal_date=signal_date, cache=cache)

        def execute(*, date, market, cache):
            if date == execution:
                execution_started.set()
            yield from original_execute(date=date, market=market, cache=cache)

        engine.submission_runner.inference = inference
        try:
            with (patch.object(engine.data_provider, "as_of", prepare),
                  patch.object(engine.accounting, "mark_at_open", open_mark),
                  patch.object(engine.optimizer, "optimize", optimize),
                  patch.object(engine.broker, "execute", execute)):
                engine.run()
        finally:
            first_opened.set()
            execution_started.set()
        self.assertEqual(self.calls, [first, second])
        self.assertEqual(len(set(model_threads)), 1)
        self.assertTrue(model_threads[0].startswith("skd-infer"))
        self.assertFalse(any(t.name.startswith(("skd-infer", "skd-data")) for t in threads()))

    def test_failed_inference_stops_queued_model_calls_and_can_restart(self):
        calls = []

        def fail(*, as_of_date, data):
            calls.append(as_of_date)
            raise RuntimeError("first prediction failed")

        engine = self.make_engine(inference=fail, rebalance_interval=1)
        with self.assertRaisesRegex(RuntimeError, "first prediction failed"):
            engine.run()
        self.assertEqual(calls, [self.dates[0]])
        self.assertIsNone(engine.metrics)
        self.assertFalse(any(t.name.startswith(("skd-infer", "skd-data")) for t in threads()))
        engine.submission_runner.inference = self.inference
        engine.run()
        self.assertEqual(engine.trading_dates, self.dates)

    def test_future_inference_error_is_reported_at_its_signal_date(self):
        first, execution, second = self.dates[:3]

        def inference(*, as_of_date, data):
            if as_of_date == second:
                raise RuntimeError("future prediction failed")
            return self.inference(as_of_date=as_of_date, data=data)

        engine = self.make_engine(inference=inference, output_dir=self.root / "future-failed")
        with protocol_components(engine) as trace, self.assertRaisesRegex(RuntimeError, "future prediction failed"):
            engine.run()
        self.assertIn(("execute", execution), trace)
        self.assertNotIn(("optimize", second), trace)
        records = [json.loads(line) for line in (engine.config.output_dir / "run.log").read_text(encoding="utf-8").splitlines()]
        error = next(record for record in records if record["level"] == "ERROR")
        self.assertEqual(error["date"], second)
        self.assertEqual(error["details"]["failed_phase"], "SIGNAL")
        self.assertEqual(error["details"]["component"], "runner.predict")
        self.assertIsNone(engine.metrics)
        self.assertFalse(any(t.name.startswith(("skd-infer", "skd-data")) for t in threads()))

    def test_empty_and_single_day_intervals(self):
        for start, end, expected in (
            ("2018-01-02", "2018-01-02", ["2018-01-02"]),
            ("2017-12-30", "2018-01-01", []),
        ):
            with self.subTest(start=start):
                engine = self.make_engine(start_date=start, end_date=end)
                engine.run()
                self.assertEqual(engine.trading_dates, expected)
                self.assertTrue(engine.tables["target_weights"].empty)
                self.assertEqual(len(engine.tables["equity_curve"]), len(expected))
                self.assertEqual(self.calls, [])
                self.assertEqual(engine.account["cash"], engine.config.initial_cash)

    def test_cost_failure_closes_generator_reader_and_records_log(self):
        output = self.root / "failed"
        engine = self.make_engine(output_dir=output)
        with (protocol_components(engine) as trace,
              patch.object(engine.cost_model, "calculate", side_effect=RuntimeError("quote failed")),
              self.assertRaisesRegex(RuntimeError, "quote failed")):
            engine.run()
        self.assertIn(("execution_closed", "2018-01-03"), trace)
        self.assertNotIn(("close", "2018-01-03"), trace)
        self.assertIsNone(engine.metrics)
        self.assertFalse(engine.tables)
        self.assertIsNone(engine.data_provider._executor)
        self.assertFalse(any(thread.name.startswith(("skd-data", "skd-infer")) for thread in threads()))
        records = [json.loads(line) for line in (output / "run.log").read_text(encoding="utf-8").splitlines()]
        errors = [record for record in records if record["level"] == "ERROR"]
        self.assertEqual(errors[-1]["phase"], "FAILED")
        self.assertIn("cost_model", errors[-1]["details"]["component"])
        self.assertEqual(errors[-1]["details"]["failed_phase"], "EXECUTION")
        self.assertFalse((output / "metrics.json").exists())
        self.assertIsNone(engine.result_writer._log)
        before = (output / "run.log").read_bytes()
        with self.assertRaises(FileExistsError):
            engine.run()
        self.assertEqual((output / "run.log").read_bytes(), before)

    def test_calendar_errors_are_explicit(self):
        broken = self.make_engine(end_date="2018-02-01", output_dir=self.root / "missing-month")
        with self.assertRaises(FileNotFoundError):
            broken.run()
        records = [json.loads(line) for line in (broken.config.output_dir / "run.log").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[-1]["details"]["component"], "data_provider.prepare")

    def test_cleanup_failure_does_not_publish_success(self):
        engine = self.make_engine()
        close = engine.result_writer.close

        def failing_close(*, cache):
            close(cache=cache)
            raise OSError("log close failed")

        with (protocol_components(engine), patch.object(engine.result_writer, "close", failing_close),
              self.assertRaisesRegex(OSError, "log close failed")):
            engine.run()
        self.assertIsNone(engine.metrics)
        self.assertEqual(engine.tables, {})
        self.assertEqual(engine.account, {})
        self.assertIsNone(engine.data_provider._executor)

    def test_invalid_predictions_and_inference_errors_stop_pipeline(self):
        bad = (
            pd.DataFrame({"date": "2018-01-02", "code": ["SH600000"], "score": [1.0]}),
            pd.DataFrame({"date": "2018-01-02", "code": ["SH600000", "SH600000"], "score": [1., 2.]}),
            pd.DataFrame({"date": "2018-01-02", "code": ["SH600000", "SZ000001"], "score": [1., float("inf")]}),
            pd.DataFrame({"date": "2099-01-01", "code": ["SH600000", "SZ000001"], "score": [1., 2.]}),
        )
        for frame in bad:
            with self.subTest(frame=frame):
                engine = self.make_engine(inference=lambda **kw: frame)
                with protocol_components(engine) as trace, self.assertRaises(ValueError):
                    engine.run()
                self.assertFalse(any(item[0] == "optimize" for item in trace))
                self.assertIsNone(engine.metrics)
                self.assertIsNone(engine.data_provider._executor)

        def inference(**kwargs):
            raise RuntimeError("model failed")
        engine = self.make_engine(inference=inference)
        with protocol_components(engine), self.assertRaisesRegex(RuntimeError, "model failed"):
            engine.run()
        self.assertIsNone(engine.metrics)


if __name__ == "__main__":
    unittest.main()
