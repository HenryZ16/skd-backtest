import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from skd_backtest.config import BacktestConfig, CostConfig, OptimizerConfig
from skd_backtest.contracts import ComponentRole, LogRecord, Phase, RunContext, Topic
from skd_backtest.result_writer import ResultWriter
from skd_backtest.schemas import METRIC_NAMES, RESULT_COLUMNS


class MemoryCache:
    def __init__(self, tables=None, metrics=None):
        backtest = BacktestConfig(
            data_dir=Path("."), start_date="2024-01-02", end_date="2024-01-03",
            initial_cash=1_000.0, rebalance_interval=1, holding_period=1, lookback=1,
            price_mode="adjusted_return", trading_days_per_year=252, risk_free_rate=0.0,
            output_dir=None,
        )
        self.values = {
            (Topic.RUN_CONTEXT, None): RunContext(backtest, OptimizerConfig(), CostConfig()),
            (Topic.EVALUATION_METRICS, None): metrics or dict.fromkeys(METRIC_NAMES),
        }
        self.tables = tables or {
            name: pd.DataFrame(columns=columns) for name, columns in RESULT_COLUMNS.items()
        }
        self.records = []
        self.published = {}

    def read(self, topic, key=None):
        return self.values[topic, key]

    def result_tables(self):
        return self.tables

    def log(self, *, level, message, details=None):
        self.records.append(LogRecord(
            len(self.records) + 1, None, Phase.OUTPUT, ComponentRole.WRITER,
            level, message, details or {},
        ))

    def log_records(self, *, after_seq):
        return self.records[after_seq:]

    def publish(self, topic, key, value):
        self.published[topic, key] = value


class ResultWriterTests(unittest.TestCase):
    def test_output_disabled_publishes_disabled_receipt_without_files(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MemoryCache()
            writer = ResultWriter(None)
            writer.open(cache=cache)
            writer.write(cache=cache)
            writer.close(cache=cache)

            receipt = cache.published[Topic.OUTPUT_RECEIPT, None]
            self.assertEqual((receipt.status, receipt.output_dir, receipt.files), ("disabled", None, {}))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_writes_metrics_and_all_seven_csvs_without_mutating_tables(self):
        tables = {
            name: pd.DataFrame(columns=columns) for name, columns in RESULT_COLUMNS.items()
        }
        tables["predictions"] = pd.DataFrame(
            [["2024-01-02", "000001", 0.25, pd.NA, "discard me"]],
            columns=(*RESULT_COLUMNS["predictions"], "extra"),
        )
        before = {name: frame.copy(deep=True) for name, frame in tables.items()}
        metrics = dict.fromkeys(METRIC_NAMES)
        metrics.update({
            "mean_rankic": pd.Series([0.125], dtype="float32").iloc[0],
            "maximum_drawdown": -0.25,
            "turnover": float("inf"),
            "transaction_cost": float("nan"),
            "failed_orders": pd.Series([3], dtype="int64").iloc[0],
        })
        cache = MemoryCache(tables=tables, metrics=metrics)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"
            writer = ResultWriter(output)
            writer.open(cache=cache)
            writer.write(cache=cache)
            writer.close(cache=cache)

            receipt = cache.published[Topic.OUTPUT_RECEIPT, None]
            self.assertEqual(receipt.status, "written")
            self.assertEqual(receipt.output_dir, str(output.resolve()))
            expected_names = {
                "metrics.json", "run.log",
                *(name + ".csv" for name in RESULT_COLUMNS),
            }
            self.assertEqual(set(receipt.files), expected_names)
            self.assertEqual(
                {name for name in output.iterdir()},
                {output / name for name in expected_names},
            )
            for name, path in receipt.files.items():
                self.assertEqual(Path(path), output / name)
                self.assertTrue(Path(path).is_absolute())

            exported_metrics = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(exported_metrics["mean_rankic"], 0.125)
            self.assertIs(type(exported_metrics["failed_orders"]), int)
            self.assertEqual(exported_metrics["failed_orders"], 3)
            self.assertEqual(exported_metrics["maximum_drawdown"], -0.25)
            self.assertIsNone(exported_metrics["turnover"])
            self.assertIsNone(exported_metrics["transaction_cost"])
            self.assertNotIn("NaN", (output / "metrics.json").read_text(encoding="utf-8"))
            self.assertNotIn("Infinity", (output / "metrics.json").read_text(encoding="utf-8"))

            for name, columns in RESULT_COLUMNS.items():
                lines = (output / (name + ".csv")).read_text(encoding="utf-8").splitlines()
                self.assertEqual(lines[0], ",".join(columns))
                if name == "predictions":
                    self.assertEqual(lines[1], "2024-01-02,000001,0.25,")
                else:
                    self.assertEqual(len(lines), 1)
            for name, frame in tables.items():
                pd.testing.assert_frame_equal(frame, before[name])

    def test_output_bytes_are_stable_across_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = []
            for index in range(2):
                output = Path(directory) / f"run-{index}"
                writer = ResultWriter(output)
                cache = MemoryCache()
                writer.open(cache=cache)
                writer.write(cache=cache)
                writer.close(cache=cache)
                outputs.append({
                    name: (output / name).read_bytes()
                    for name in ("metrics.json", "run.log", *(table + ".csv" for table in RESULT_COLUMNS))
                })
            self.assertEqual(outputs[0], outputs[1])

    def test_open_rejects_existing_protocol_output_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"
            output.mkdir()
            existing = output / "orders.csv"
            existing.write_text("keep this file", encoding="utf-8")
            writer = ResultWriter(output)

            with self.assertRaises(FileExistsError):
                writer.open(cache=MemoryCache())

            self.assertEqual(existing.read_text(encoding="utf-8"), "keep this file")
            self.assertFalse((output / "run.log").exists())

    def test_csv_failure_leaves_no_metrics_receipt_or_temporary_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"
            cache = MemoryCache()
            writer = ResultWriter(output)
            writer.open(cache=cache)
            original_to_csv = pd.DataFrame.to_csv
            calls = 0

            def fail_second_csv(frame, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected CSV failure")
                return original_to_csv(frame, *args, **kwargs)

            with patch.object(pd.DataFrame, "to_csv", new=fail_second_csv):
                with self.assertRaisesRegex(OSError, "injected CSV failure"):
                    writer.write(cache=cache)
            writer.close(cache=cache)

            self.assertFalse((output / "metrics.json").exists())
            self.assertNotIn((Topic.OUTPUT_RECEIPT, None), cache.published)
            self.assertEqual(list(output.glob(".skd-backtest-*")), [])
            self.assertFalse(any((output / (name + ".csv")).exists() for name in RESULT_COLUMNS))

    def test_receipt_publish_failure_removes_completion_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"
            cache = MemoryCache()
            writer = ResultWriter(output)
            writer.open(cache=cache)
            publish = cache.publish

            def fail_receipt(topic, key, value):
                if topic == Topic.OUTPUT_RECEIPT:
                    raise OSError("receipt publication failed")
                publish(topic, key, value)

            cache.publish = fail_receipt
            with self.assertRaisesRegex(OSError, "receipt publication failed"):
                writer.write(cache=cache)
            writer.close(cache=cache)

            self.assertFalse((output / "metrics.json").exists())
            self.assertNotIn((Topic.OUTPUT_RECEIPT, None), cache.published)
            self.assertEqual(list(output.glob(".skd-backtest-*")), [])
    def test_close_failure_removes_only_the_completion_marker(self):
        class Sink:
            def __init__(self, failure):
                self.failure = failure
                self.closed = False

            def write(self, value):
                return len(value)

            def flush(self):
                if self.failure == "flush":
                    raise OSError("close flush failed")

            def close(self):
                self.closed = True
                if self.failure == "close":
                    raise OSError("log close failed")

        for failure in ("flush", "close"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "results"
                cache = MemoryCache()
                writer = ResultWriter(output)
                writer.open(cache=cache)
                writer.write(cache=cache)
                original_log = writer._log
                sink = Sink(failure)
                writer._log = sink
                if failure == "flush":
                    cache.log(level="DEBUG", message="pending log record")

                with self.assertRaises(OSError):
                    writer.close(cache=cache)

                self.assertFalse((output / "metrics.json").exists())
                self.assertTrue((output / "run.log").exists())
                self.assertTrue(all((output / (name + ".csv")).exists() for name in RESULT_COLUMNS))
                self.assertTrue(sink.closed)
                self.assertIsNone(writer._log)
                original_log.close()
    def test_log_cursor_advances_only_after_flush_and_close_attempts_release(self):
        class Sink:
            def __init__(self):
                self.fail_flush = False
                self.closed = False

            def write(self, value):
                return len(value)

            def flush(self):
                if self.fail_flush:
                    raise OSError("flush failed")

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            writer = ResultWriter(Path(directory) / "results")
            cache = MemoryCache()
            writer.open(cache=cache)
            original_log = writer._log
            sink = Sink()
            writer._log = sink
            cache.log(level="DEBUG", message="record two")
            sink.fail_flush = True

            with self.assertRaisesRegex(OSError, "flush failed"):
                writer.flush_log(cache=cache)
            self.assertEqual(writer._cursor, 1)

            sink.fail_flush = False
            writer.flush_log(cache=cache)
            self.assertEqual(writer._cursor, 2)

            cache.log(level="DEBUG", message="record three")
            sink.fail_flush = True
            with self.assertRaisesRegex(OSError, "flush failed"):
                writer.close(cache=cache)
            self.assertEqual(writer._cursor, 2)
            self.assertTrue(sink.closed)
            self.assertIsNone(writer._log)
            self.assertFalse(writer._opened)
            original_log.close()

    def test_open_failure_closes_log_handle(self):
        class Sink:
            def __init__(self, fail_close):
                self.closed = False
                self.fail_close = fail_close

            def close(self):
                self.closed = True
                if self.fail_close:
                    raise OSError("log close failed")

        class BrokenCache(MemoryCache):
            def read(self, topic, key=None):
                raise RuntimeError("context read failed")

        for fail_close in (False, True):
            with self.subTest(fail_close=fail_close), tempfile.TemporaryDirectory() as directory:
                writer = ResultWriter(Path(directory) / "results")
                cache = BrokenCache()
                sink = Sink(fail_close)
                with patch.object(Path, "open", return_value=sink):
                    if fail_close:
                        with self.assertRaisesRegex(OSError, "log close failed"):
                            writer.open(cache=cache)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "context read failed"):
                            writer.open(cache=cache)
                self.assertTrue(sink.closed)
                self.assertIsNone(writer._log)
                self.assertFalse(writer._opened)

if __name__ == "__main__":
    unittest.main()