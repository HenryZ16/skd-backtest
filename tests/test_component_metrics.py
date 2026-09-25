"""Hand-calculated checks for the Metrics component."""

import unittest
from copy import deepcopy

import pandas as pd
from pandas.testing import assert_frame_equal

from skd_backtest.contracts import Topic
from skd_backtest.metrics import Metrics
from skd_backtest.schemas import METRIC_NAMES, RESULT_COLUMNS, empty_result


class MemoryCache:
    def __init__(self, tables):
        self.tables = tables
        self.result_tables_calls = 0
        self.published = {}

    def result_tables(self):
        self.result_tables_calls += 1
        return self.tables

    def publish(self, topic, key, value):
        self.published[topic, key] = value


def make_tables(*, rankic=(), equity=(), orders=()):
    tables = {name: empty_result(name) for name in RESULT_COLUMNS}
    tables["rankic"] = pd.DataFrame(rankic, columns=RESULT_COLUMNS["rankic"])
    tables["equity_curve"] = pd.DataFrame(equity, columns=RESULT_COLUMNS["equity_curve"])
    tables["orders"] = pd.DataFrame(orders, columns=RESULT_COLUMNS["orders"])
    return tables


def equity_row(date, portfolio_return, portfolio_nav, *, benchmark_return=None,
               benchmark_nav=None, active_return=None, turnover=0.0,
               transaction_cost=0.0):
    return {
        "date": date, "cash": 0.0, "market_value": 0.0,
        "portfolio_value": 0.0, "portfolio_nav": portfolio_nav,
        "portfolio_return": portfolio_return, "benchmark_nav": benchmark_nav,
        "benchmark_return": benchmark_return, "active_return": active_return,
        "turnover": turnover, "transaction_cost": transaction_cost,
    }


class MetricsTests(unittest.TestCase):
    def calculate(self, tables, *, trading_days_per_year=2, risk_free_rate=0.0):
        cache = MemoryCache(tables)
        Metrics(trading_days_per_year, risk_free_rate).calculate(cache=cache)
        self.assertEqual(cache.result_tables_calls, 1)
        self.assertEqual(list(cache.published), [(Topic.EVALUATION_METRICS, None)])
        return cache.published[Topic.EVALUATION_METRICS, None]

    def test_rankic_sample_statistics_and_portfolio_sample_risk(self):
        metrics = self.calculate(make_tables(
            rankic=[("2024-01-02", 0.2, 2), ("2024-01-03", -0.2, 2)],
            equity=[
                equity_row("2024-01-02", 0.1, 1.1, turnover=0.1),
                equity_row("2024-01-03", 0.3, 1.43, turnover=0.2),
            ],
        ))
        self.assertEqual(tuple(metrics), METRIC_NAMES)
        self.assertAlmostEqual(metrics["mean_rankic"], 0.0)
        self.assertAlmostEqual(metrics["rankic_std"], (0.08) ** 0.5)
        self.assertAlmostEqual(metrics["rankic_ir"], 0.0)
        self.assertAlmostEqual(metrics["positive_rankic_ratio"], 0.5)
        self.assertAlmostEqual(metrics["total_return"], 0.43)
        self.assertAlmostEqual(metrics["annualized_return"], 0.43)
        self.assertAlmostEqual(metrics["annualized_volatility"], 0.2)
        self.assertAlmostEqual(metrics["sharpe_ratio"], 2.0)
        self.assertAlmostEqual(metrics["turnover"], 0.3)

    def test_first_day_loss_is_in_maximum_drawdown(self):
        metrics = self.calculate(make_tables(equity=[
            equity_row("2024-01-02", -0.1, 0.9),
        ]), trading_days_per_year=1)
        self.assertAlmostEqual(metrics["total_return"], -0.1)
        self.assertAlmostEqual(metrics["annualized_return"], -0.1)
        self.assertAlmostEqual(metrics["maximum_drawdown"], 0.1)
        self.assertIsNone(metrics["annualized_volatility"])

    def test_sharpe_uses_compounded_daily_risk_free_rate(self):
        metrics = self.calculate(make_tables(equity=[
            equity_row("2024-01-02", 0.3, 1.3),
            equity_row("2024-01-03", 0.5, 1.95),
        ]), trading_days_per_year=2, risk_free_rate=0.44)
        self.assertAlmostEqual((1 + 0.44) ** (1 / 2) - 1, 0.2)
        self.assertAlmostEqual(metrics["sharpe_ratio"], 2.0)

    def test_benchmark_metrics_use_compounded_nav_and_active_returns(self):
        metrics = self.calculate(make_tables(equity=[
            equity_row("2024-01-02", 0.1, 1.1, benchmark_return=0.0,
                       benchmark_nav=1.0, active_return=0.1),
            equity_row("2024-01-03", 0.3, 1.43, benchmark_return=0.1,
                       benchmark_nav=1.1, active_return=0.2),
        ]))
        self.assertAlmostEqual(metrics["annualized_excess_return"], 0.33)
        self.assertAlmostEqual(metrics["tracking_error"], 0.1)
        self.assertAlmostEqual(metrics["information_ratio"], 3.0)

    def test_missing_benchmark_keeps_all_benchmark_metrics_none(self):
        metrics = self.calculate(make_tables(equity=[
            equity_row("2024-01-02", 0.1, 1.1),
            equity_row("2024-01-03", 0.2, 1.32),
        ]))
        for name in ("annualized_excess_return", "tracking_error", "information_ratio"):
            self.assertIsNone(metrics[name])

    def test_zero_near_zero_variance_and_small_samples(self):
        constant = self.calculate(make_tables(
            rankic=[("2024-01-02", 0.25, 2), ("2024-01-03", 0.25, 2)],
            equity=[
                equity_row("2024-01-02", 0.1, 1.1, benchmark_return=0.05,
                           benchmark_nav=1.05, active_return=0.05),
                equity_row("2024-01-03", 0.1, 1.21, benchmark_return=0.05,
                           benchmark_nav=1.1025, active_return=0.05),
            ],
        ))
        self.assertEqual(constant["rankic_std"], 0.0)
        self.assertIsNone(constant["rankic_ir"])
        self.assertEqual(constant["annualized_volatility"], 0.0)
        self.assertIsNone(constant["sharpe_ratio"])
        self.assertEqual(constant["tracking_error"], 0.0)
        self.assertIsNone(constant["information_ratio"])

        near_constant = self.calculate(make_tables(equity=[
            equity_row("2024-01-02", 0.01, 1.01),
            equity_row("2024-01-03", 0.010000000000000002, 1.0201),
        ]))
        self.assertEqual(near_constant["annualized_volatility"], 0.0)
        self.assertIsNone(near_constant["sharpe_ratio"])

        one_day = self.calculate(make_tables(
            rankic=[("2024-01-02", 0.2, 2)],
            equity=[equity_row("2024-01-02", 0.1, 1.1)],
        ), trading_days_per_year=1)
        self.assertIsNone(one_day["rankic_std"])
        self.assertIsNone(one_day["rankic_ir"])
        self.assertIsNone(one_day["annualized_volatility"])
        self.assertIsNone(one_day["sharpe_ratio"])

    def test_empty_tables_return_none_for_period_metrics_and_zero_for_trading_totals(self):
        metrics = self.calculate(make_tables())
        for name in METRIC_NAMES[:12]:
            self.assertIsNone(metrics[name])
        self.assertEqual(metrics["turnover"], 0.0)
        self.assertEqual(metrics["transaction_cost"], 0.0)
        self.assertEqual(metrics["failed_orders"], 0)

    def test_only_rejected_orders_count_and_audit_tables_are_unchanged(self):
        tables = make_tables(
            equity=[
                equity_row("2024-01-02", 0.01, 1.01, turnover=0.1,
                           transaction_cost=2.0),
                equity_row("2024-01-03", 0.02, 1.0302, turnover=0.2,
                           transaction_cost=3.0),
            ],
            orders=[
                {"date": "2024-01-02", "code": "A", "side": "BUY",
                 "requested_shares": 1, "status": "REJECTED"},
                {"date": "2024-01-02", "code": "B", "side": "BUY",
                 "requested_shares": 1, "status": "PARTIALLY_FILLED"},
                {"date": "2024-01-03", "code": "C", "side": "BUY",
                 "requested_shares": 1, "status": "FILLED"},
            ],
        )
        before = {name: frame.copy(deep=True) for name, frame in tables.items()}
        metrics = self.calculate(tables)
        self.assertEqual(metrics["failed_orders"], 1)
        self.assertAlmostEqual(metrics["turnover"], 0.3)
        self.assertAlmostEqual(metrics["transaction_cost"], 5.0)
        for name, frame in tables.items():
            assert_frame_equal(frame, before[name])

    def test_constructor_validates_annualization_and_risk_free_rate(self):
        for days, rate in ((0, 0.0), (True, 0.0), (252, -1.0),
                           (252, float("nan")), (252, float("inf"))):
            with self.subTest(days=days, rate=rate), self.assertRaises(ValueError):
                Metrics(days, rate)


if __name__ == "__main__":
    unittest.main()
