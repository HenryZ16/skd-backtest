"""Focused checks for evaluator-only labels and their independent price reads."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from skd_backtest.config import BacktestConfig, CostConfig, DataCapabilities, OptimizerConfig
from skd_backtest.contracts import RunContext, Topic
from skd_backtest.label_provider import LabelProvider
from skd_backtest.schemas import LABEL_COLUMNS


class LabelCache:
    def __init__(self, scores, context):
        self.scores = scores
        self.context = context
        self.published = {}

    def history(self, topic, *, date=None):
        assert topic == Topic.SIGNAL_SCORES
        return self.scores

    def read(self, topic, key=None):
        assert topic == Topic.RUN_CONTEXT and key is None
        return self.context

    def publish(self, topic, key, value):
        self.published[(topic, key)] = value


def market_row(day, code, adjusted_open, *, raw_open=None, factor=1.0, suspended=False):
    return {
        "日期": int(day.replace("-", "")),
        "代码": code,
        "open": adjusted_open,
        "raw_open": adjusted_open if raw_open is None else raw_open,
        "adjustment_factor": factor,
        "is_suspend": suspended,
    }


def make_config(
    data_dir, *, start_date="2024-01-01", end_date="2024-01-31", holding_period=1,
    basis="adjusted_open", raw_prices=False, adjustment_factors=False,
):
    return BacktestConfig(
        data_dir=data_dir,
        start_date=start_date,
        end_date=end_date,
        initial_cash=1_000_000.0,
        rebalance_interval=1,
        holding_period=holding_period,
        lookback=1,
        price_mode="adjusted_return",
        trading_days_per_year=252,
        risk_free_rate=0.0,
        output_dir=None,
        label_price_basis=basis,
        data_capabilities=DataCapabilities(
            raw_prices=raw_prices,
            adjustment_factors=adjustment_factors,
        ),
    )


def run_labels(data_dir, monthly_rows, scores, config):
    frames = {}
    for month, rows in monthly_rows.items():
        year, number = month
        path = Path(data_dir) / "MarketData" / str(year) / f"{number:02d}" / f"{year}{number:02d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        frames[month] = pd.DataFrame(rows)

    calls = []

    def read_parquet(path, *, columns, filters=None):
        month = (int(path.parent.parent.name), int(path.parent.name))
        calls.append((month, list(columns), filters))
        table = frames[month]
        if filters:
            for column, operator, values in filters:
                assert operator == "in"
                table = table.loc[table[column].isin(values)]
        return table.loc[:, columns].copy()

    cache = LabelCache(
        scores,
        RunContext(config, OptimizerConfig(), CostConfig()),
    )
    with patch("skd_backtest.label_provider.pd.read_parquet", side_effect=read_parquet):
        LabelProvider(config).build(cache=cache)
    return cache.published[(Topic.EVALUATION_LABELS, None)], calls


class LabelProviderTest(unittest.TestCase):
    def test_cross_month_weekend_and_h_offset_use_full_market_calendar(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            scores = pd.DataFrame(
                [
                    {"date": "2024-01-31", "code": "600000SH", "score": 1.0},
                    {"date": "2024-02-02", "code": "600000SH", "score": 2.0},
                ]
            )
            original = scores.copy(deep=True)
            rows = [
                market_row("2024-01-31", "600000SH", 9.0),
                market_row("2024-02-01", "600000SH", 10.0),
                market_row("2024-02-02", "600000SH", 12.0),
                market_row("2024-02-05", "600000SH", 20.0),
                market_row("2024-02-06", "600000SH", 24.0),
            ]
            labels, calls = run_labels(
                temp_dir,
                {(2024, 1): rows[:1], (2024, 2): rows[1:]},
                scores,
                make_config(temp_dir, end_date="2024-02-02", holding_period=1),
            )

            self.assertEqual(tuple(labels.columns), LABEL_COLUMNS)
            self.assertEqual(labels["entry_date"].tolist(), ["2024-02-01", "2024-02-05"])
            self.assertEqual(labels["exit_date"].tolist(), ["2024-02-02", "2024-02-06"])
            self.assertAlmostEqual(labels["future_return"].iloc[0], 0.2)
            self.assertAlmostEqual(labels["future_return"].iloc[1], 0.2)
            self.assertEqual(labels["missing_reason"].tolist(), [None, None])
            pd.testing.assert_frame_equal(scores, original)
            self.assertTrue(any(month == (2024, 2) and "open" in columns for month, columns, _ in calls))
            self.assertTrue(all(
                set(columns) == {"日期"} or set(columns) <= {"日期", "代码", "open", "is_suspend"}
                for _, columns, _ in calls
            ))

    def test_reads_forward_prices_after_backtest_end(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            scores = pd.DataFrame([{"date": "2024-01-31", "code": "A", "score": 1.0}])
            rows = [
                market_row("2024-01-31", "A", 9.0),
                market_row("2024-02-01", "A", 10.0),
                market_row("2024-02-02", "A", 12.0),
            ]
            labels, calls = run_labels(
                temp_dir,
                {(2024, 1): rows[:1], (2024, 2): rows[1:]},
                scores,
                make_config(temp_dir, end_date="2024-01-31", holding_period=1),
            )

            self.assertEqual(labels.loc[0, "exit_date"], "2024-02-02")
            self.assertAlmostEqual(labels.loc[0, "future_return"], 0.2)
            self.assertTrue(any(month == (2024, 2) and filters for month, _, filters in calls))

    def test_missing_stock_endpoint_and_suspension_keep_prediction_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            scores = pd.DataFrame(
                [
                    {"date": "2024-01-02", "code": "B", "score": 1.0},
                    {"date": "2024-01-02", "code": "C", "score": 2.0},
                ]
            )
            rows = [
                market_row("2024-01-02", "A", 10.0),
                market_row("2024-01-03", "A", 11.0),
                market_row("2024-01-04", "A", 12.0),
                market_row("2024-01-02", "B", 10.0),
                market_row("2024-01-03", "B", 11.0),
                market_row("2024-01-02", "C", 10.0),
                market_row("2024-01-03", "C", 11.0),
                market_row("2024-01-04", "C", 12.0, suspended=True),
            ]
            labels, _ = run_labels(
                temp_dir,
                {(2024, 1): rows},
                scores,
                make_config(temp_dir, end_date="2024-01-04"),
            )

            self.assertEqual(labels["code"].tolist(), ["B", "C"])
            self.assertEqual(labels["future_return"].tolist(), [None, None])
            self.assertEqual(labels["missing_reason"].tolist(), ["EXIT_MISSING", "EXIT_SUSPENDED"])

    def test_dataset_tail_marks_missing_exit_as_tail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            scores = pd.DataFrame([{"date": "2024-01-02", "code": "A", "score": 1.0}])
            rows = [
                market_row("2024-01-02", "A", 9.0),
                market_row("2024-01-03", "A", 10.0),
            ]
            labels, _ = run_labels(
                temp_dir,
                {(2024, 1): rows},
                scores,
                make_config(temp_dir, end_date="2024-01-02", holding_period=1),
            )

            self.assertEqual(labels.loc[0, "entry_date"], "2024-01-03")
            self.assertIsNone(labels.loc[0, "exit_date"])
            self.assertIsNone(labels.loc[0, "future_return"])
            self.assertEqual(labels.loc[0, "missing_reason"], "TAIL")

    def test_raw_open_preferred_and_factor_reconstructs_raw_price(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            scores = pd.DataFrame([{"date": "2024-01-02", "code": "A", "score": 1.0}])
            rows = [
                market_row("2024-01-02", "A", 90.0),
                market_row("2024-01-03", "A", 100.0, raw_open=10.0, factor=10.0),
                market_row("2024-01-04", "A", 144.0, raw_open=12.0, factor=12.0),
            ]
            labels, calls = run_labels(
                temp_dir,
                {(2024, 1): rows},
                scores,
                make_config(
                    temp_dir, end_date="2024-01-02", basis="raw_open",
                    raw_prices=True, adjustment_factors=True,
                ),
            )
            self.assertAlmostEqual(labels.loc[0, "future_return"], 0.2)
            endpoint_columns = [columns for _, columns, filters in calls if filters]
            self.assertTrue(endpoint_columns)
            self.assertTrue(all("raw_open" in columns and "adjustment_factor" not in columns
                                for columns in endpoint_columns))

        with tempfile.TemporaryDirectory() as temp_dir:
            scores = pd.DataFrame([{"date": "2024-01-02", "code": "A", "score": 1.0}])
            rows = [
                market_row("2024-01-02", "A", 90.0, factor=9.0),
                market_row("2024-01-03", "A", 100.0, factor=10.0),
                market_row("2024-01-04", "A", 144.0, factor=12.0),
            ]
            labels, calls = run_labels(
                temp_dir,
                {(2024, 1): rows},
                scores,
                make_config(
                    temp_dir, end_date="2024-01-02", basis="raw_open",
                    adjustment_factors=True,
                ),
            )
            self.assertAlmostEqual(labels.loc[0, "future_return"], 0.2)
            endpoint_columns = [columns for _, columns, filters in calls if filters]
            self.assertTrue(all("adjustment_factor" in columns for columns in endpoint_columns))

    def test_missing_required_month_is_an_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            scores = pd.DataFrame([{"date": "2024-01-02", "code": "A", "score": 1.0}])
            months = {
                (2024, 1): [
                    market_row("2024-01-02", "A", 9.0),
                    market_row("2024-01-03", "A", 10.0),
                ],
                (2024, 3): [market_row("2024-03-01", "A", 11.0)],
            }
            for (year, number), rows in months.items():
                path = Path(temp_dir) / "MarketData" / str(year) / f"{number:02d}" / f"{year}{number:02d}.parquet"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"")
            frames = {month: pd.DataFrame(rows) for month, rows in months.items()}

            def read_parquet(path, *, columns, filters=None):
                month = (int(path.parent.parent.name), int(path.parent.name))
                table = frames[month]
                if filters:
                    for column, operator, values in filters:
                        self.assertEqual(operator, "in")
                        table = table.loc[table[column].isin(values)]
                return table.loc[:, columns].copy()

            config = make_config(temp_dir, holding_period=1)
            cache = LabelCache(scores, RunContext(config, OptimizerConfig(), CostConfig()))
            with patch("skd_backtest.label_provider.pd.read_parquet", side_effect=read_parquet):
                with self.assertRaisesRegex(FileNotFoundError, "202402"):
                    LabelProvider(config).build(cache=cache)

    def test_empty_scores_publish_full_empty_schema_without_reading_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_config(temp_dir)
            cache = LabelCache(pd.DataFrame(columns=["date", "code", "score"]),
                               RunContext(config, OptimizerConfig(), CostConfig()))
            with patch("skd_backtest.label_provider.pd.read_parquet") as read:
                LabelProvider(config).build(cache=cache)

            output = cache.published[(Topic.EVALUATION_LABELS, None)]
            self.assertTrue(output.empty)
            self.assertEqual(tuple(output.columns), LABEL_COLUMNS)
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
