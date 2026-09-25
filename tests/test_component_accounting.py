from pathlib import Path
import unittest

import pandas as pd

from skd_backtest.accounting import PortfolioAccounting
from skd_backtest.config import BacktestConfig
from skd_backtest.contracts import (
    AccountState, BenchmarkDay, Dataset, ExecutionResult, InitialAccount,
    OpenSnapshot, RunCalendar, Topic,
)
from skd_backtest.schemas import LOCK_COLUMNS, RESULT_COLUMNS, STATE_COLUMNS, VALUE_COLUMNS


class MemoryCache:
    def __init__(self, values=None):
        self.values = values or {}

    def read(self, topic, key=None):
        return self.values[(topic, key)]

    def publish(self, topic, key, value):
        self.values[(topic, key)] = value


def _config(price_mode="adjusted_return", benchmark_mode="none", initial_cash=1_000):
    return BacktestConfig(
        data_dir=Path("."), start_date="2024-01-02", end_date="2024-01-03",
        initial_cash=initial_cash, rebalance_interval=1, holding_period=1, lookback=1,
        price_mode=price_mode, trading_days_per_year=252, risk_free_rate=0.0,
        output_dir=None, benchmark_mode=benchmark_mode,
    )


def _account(mode, cash, positions):
    return AccountState(
        mode, cash, pd.DataFrame(positions, columns=STATE_COLUMNS[mode]),
        pd.DataFrame(columns=LOCK_COLUMNS),
    )


def _execution(date, account, trade_value=0.0, total_cost=0.0, trades=None):
    return ExecutionResult(
        date, account, pd.DataFrame(columns=RESULT_COLUMNS["orders"]),
        trades if trades is not None else pd.DataFrame(columns=RESULT_COLUMNS["trades"]),
        trade_value, total_cost, None,
    )


def _calendar(*dates):
    return RunCalendar(tuple(dates), pd.DataFrame(columns=["signal_date", "execution_date"]))


def _close_inputs(date, account, opening, execution, *, dates, initial_cash, benchmark=None,
                  previous_close=None):
    values = {
        (Topic.RUN_CALENDAR, None): _calendar(*dates),
        (Topic.ACCOUNT_INITIAL, None): InitialAccount(
            _account(account.price_mode, initial_cash, []), initial_cash, 1.0,
            1.0 if benchmark is not None else None,
        ),
        (Topic.ACCOUNT_OPEN, date): opening,
        (Topic.EXECUTION_DAY, date): execution,
        (Topic.REFERENCE_BENCHMARK, date): benchmark or Dataset("unavailable", None, "disabled"),
    }
    if previous_close is not None:
        previous_date, snapshot = previous_close
        values[(Topic.ACCOUNT_CLOSE, previous_date)] = snapshot
    return MemoryCache(values)


def _mark_close(accounting, cache, date, market):
    accounting.mark_to_market(date=date, market=market, cache=cache)
    return cache.read(Topic.ACCOUNT_CLOSE, date)


class TestPortfolioAccounting(unittest.TestCase):
    def test_open_and_close_use_separate_adjusted_prices(self):
        date = "2024-01-02"
        account = _account("adjusted_return", 900, [{
            "code": "A", "position_value": 100.0, "reference_price": 10.0,
            "reference_date": "2024-01-01",
        }])
        cache = MemoryCache({(Topic.ACCOUNT_SETTLED, date): account})
        accounting = PortfolioAccounting(_config())

        accounting.mark_at_open(date=date, market=pd.DataFrame([{
            "code": "A", "adjusted_open": 20.0, "adjusted_close": 70.0,
            "is_suspended": False, "is_missing": False,
        }]), cache=cache)

        opening = cache.read(Topic.ACCOUNT_OPEN, date)
        self.assertEqual(opening.market_value, 200.0)
        self.assertEqual(opening.values.loc[0, "price"], 20.0)
        self.assertEqual(opening.account.positions.loc[0, "position_value"], 200.0)
        self.assertEqual(account.positions.loc[0, "position_value"], 100.0)

        execution = _execution(date, opening.account)
        close_cache = _close_inputs(
            date, opening.account, opening, execution, dates=(date,), initial_cash=1_000,
        )
        snapshot = _mark_close(accounting, close_cache, date, pd.DataFrame([{
            "code": "A", "adjusted_open": 20.0, "adjusted_close": 22.0,
            "is_suspended": False, "is_missing": False,
        }]))
        self.assertEqual(snapshot.positions.loc[0, "close"], 22.0)
        self.assertEqual(snapshot.positions.loc[0, "market_value"], 220.0)
        self.assertTrue(pd.isna(snapshot.positions.loc[0, "shares"]))
        self.assertTrue(pd.isna(snapshot.positions.loc[0, "sellable_shares"]))
        self.assertAlmostEqual(snapshot.actual_weights.loc[0, "weight"], 220.0 / 1_120.0)
        self.assertEqual(snapshot.account.positions.loc[0, "reference_date"], date)

    def test_gaps_use_recent_history_and_same_day_open_reference(self):
        date = "2024-01-03"
        market = pd.DataFrame([{
            "code": "A", "adjusted_open": None, "is_suspended": True, "is_missing": False,
            "previous_close": 12.0, "previous_close_date": 20240102,
        }])
        accounting = PortfolioAccounting(_config())

        older = _account("adjusted_return", 0, [{
            "code": "A", "position_value": 100.0, "reference_price": 10.0,
            "reference_date": "2024-01-01",
        }])
        cache = MemoryCache({(Topic.ACCOUNT_SETTLED, date): older})
        accounting.mark_at_open(date=date, market=market, cache=cache)
        opening = cache.read(Topic.ACCOUNT_OPEN, date)
        self.assertEqual(opening.market_value, 120.0)
        self.assertEqual(opening.values.loc[0, "price_date"], "2024-01-02")

        adjusted = _account("adjusted_return", 0, [{
            "code": "A", "position_value": 80.0, "reference_price": 8.0,
            "reference_date": date,
        }])
        current_cache = MemoryCache({(Topic.ACCOUNT_SETTLED, date): adjusted})
        accounting.mark_at_open(date=date, market=market, cache=current_cache)
        current_open = current_cache.read(Topic.ACCOUNT_OPEN, date)
        self.assertEqual(current_open.market_value, 80.0)
        self.assertEqual(current_open.values.loc[0, "price"], 8.0)

    def test_raw_mode_values_real_shares_without_adjusted_price_fallback(self):
        date = "2024-01-02"
        account = _account("raw_price", 100, [{
            "code": "A", "total_shares": 10, "sellable_shares": 5,
            "reference_price": 10.0, "reference_date": "2024-01-01",
        }])
        cache = MemoryCache({(Topic.ACCOUNT_SETTLED, date): account})
        accounting = PortfolioAccounting(_config("raw_price", initial_cash=200))

        accounting.mark_at_open(date=date, market=pd.DataFrame([{
            "code": "A", "raw_open": 20.0, "adjusted_open": 5.0, "raw_close": 90.0,
            "is_suspended": False, "is_missing": False,
        }]), cache=cache)
        opening = cache.read(Topic.ACCOUNT_OPEN, date)
        self.assertEqual(opening.market_value, 200.0)
        self.assertEqual(opening.values.loc[0, "price"], 20.0)
        self.assertEqual(opening.values.loc[0, "market_value"], 200.0)

        cache = _close_inputs(
            date, opening.account, opening, _execution(date, opening.account),
            dates=(date,), initial_cash=200,
        )
        snapshot = _mark_close(accounting, cache, date, pd.DataFrame([{
            "code": "A", "raw_close": 22.0, "adjusted_close": 1.0,
            "is_suspended": False, "is_missing": False,
        }]))
        self.assertEqual(snapshot.positions.loc[0, "shares"], 10)
        self.assertEqual(snapshot.positions.loc[0, "sellable_shares"], 5)
        self.assertEqual(snapshot.positions.loc[0, "close"], 22.0)
        self.assertEqual(snapshot.positions.loc[0, "market_value"], 220.0)
        self.assertAlmostEqual(snapshot.equity_curve.loc[0, "portfolio_return"], 0.6)

    def test_cost_turnover_daily_return_and_benchmark_nav_compound_from_cache(self):
        first, second = "2024-01-02", "2024-01-03"
        accounting = PortfolioAccounting(_config(benchmark_mode="csi300"))

        empty = _account("adjusted_return", 1_000, [])
        cache = MemoryCache({
            (Topic.ACCOUNT_SETTLED, first): empty,
            (Topic.RUN_CALENDAR, None): _calendar(first, second),
            (Topic.ACCOUNT_INITIAL, None): InitialAccount(empty, 1_000, 1.0, 1.0),
            (Topic.REFERENCE_BENCHMARK, first): Dataset("available", BenchmarkDay(first, 0.10)),
        })
        accounting.mark_at_open(date=first, market=pd.DataFrame(columns=["code"]), cache=cache)
        opening = cache.read(Topic.ACCOUNT_OPEN, first)
        after_trade = _account("adjusted_return", 899, [{
            "code": "A", "position_value": 100.0, "reference_price": 10.0,
            "reference_date": first,
        }])
        trade = pd.DataFrame([{
            "date": first, "code": "A", "side": "BUY", "shares": None, "price": None,
            "trade_value": 100.0, "commission": 1.0, "stamp_tax": 0.0, "other_cost": 0.0,
            "total_cost": 1.0, "order_id": "2024-01-02-0001", "signal_date": "2024-01-01",
            "position_value": 100.0,
        }], columns=RESULT_COLUMNS["trades"])
        cache.publish(Topic.EXECUTION_DAY, first, _execution(first, after_trade, 100.0, 1.0, trade))
        first_close = _mark_close(accounting, cache, first, pd.DataFrame([{
            "code": "A", "adjusted_close": 11.0, "is_suspended": False, "is_missing": False,
        }]))
        row = first_close.equity_curve.iloc[0]
        self.assertEqual(row["portfolio_value"], 1_009.0)
        self.assertAlmostEqual(row["portfolio_return"], 0.009)
        self.assertAlmostEqual(row["portfolio_nav"], 1.009)
        self.assertAlmostEqual(row["benchmark_nav"], 1.10)
        self.assertAlmostEqual(row["active_return"], 0.009 - 0.10)
        self.assertAlmostEqual(row["turnover"], 0.10)
        self.assertEqual(row["transaction_cost"], 1.0)

        second_account = first_close.account
        second_cache = MemoryCache({
            (Topic.ACCOUNT_SETTLED, second): second_account,
            (Topic.RUN_CALENDAR, None): _calendar(first, second),
            (Topic.ACCOUNT_INITIAL, None): InitialAccount(empty, 1_000, 1.0, 1.0),
            (Topic.ACCOUNT_CLOSE, first): first_close,
            (Topic.REFERENCE_BENCHMARK, second): Dataset("available", BenchmarkDay(second, 0.05)),
        })
        accounting.mark_at_open(date=second, market=pd.DataFrame([{
            "code": "A", "adjusted_open": 11.0, "is_suspended": False, "is_missing": False,
        }]), cache=second_cache)
        second_open = second_cache.read(Topic.ACCOUNT_OPEN, second)
        second_cache.publish(Topic.EXECUTION_DAY, second, _execution(second, second_open.account))
        second_close = _mark_close(accounting, second_cache, second, pd.DataFrame([{
            "code": "A", "adjusted_close": 12.0, "is_suspended": False, "is_missing": False,
        }]))
        row = second_close.equity_curve.iloc[0]
        self.assertAlmostEqual(row["portfolio_return"], 10.0 / 1_009.0)
        self.assertAlmostEqual(row["benchmark_nav"], 1.155)
        self.assertEqual(row["turnover"], 0.0)
        self.assertEqual(row["transaction_cost"], 0.0)

    def test_empty_account_and_no_trade_still_publish_one_equity_row(self):
        date = "2024-01-02"
        account = _account("adjusted_return", 1_000, [])
        cache = _close_inputs(
            date, account, OpenSnapshot(date, account, pd.DataFrame(columns=VALUE_COLUMNS), 0.0, 1_000),
            _execution(date, account), dates=(date,), initial_cash=1_000,
        )
        snapshot = _mark_close(
            PortfolioAccounting(_config()), cache, date, pd.DataFrame(columns=["code"]),
        )
        self.assertEqual(len(snapshot.equity_curve), 1)
        self.assertTrue(snapshot.positions.empty)
        self.assertTrue(snapshot.actual_weights.empty)
        row = snapshot.equity_curve.iloc[0]
        self.assertEqual(row["market_value"], 0.0)
        self.assertEqual(row["portfolio_value"], 1_000.0)
        self.assertEqual(row["portfolio_return"], 0.0)
        self.assertTrue(pd.isna(row["benchmark_nav"]))
        self.assertTrue(pd.isna(row["benchmark_return"]))
        self.assertTrue(pd.isna(row["active_return"]))
        self.assertEqual(row["turnover"], 0.0)
        self.assertEqual(row["transaction_cost"], 0.0)

    def test_enabled_benchmark_rejects_missing_or_invalid_inputs(self):
        date = "2024-01-02"
        cases = (
            (Dataset("unavailable", None, "benchmark source missing"), 1.0,
             "benchmark data unavailable"),
            (Dataset("available", BenchmarkDay(date, float("nan"))), 1.0,
             "invalid benchmark return"),
            (Dataset("available", BenchmarkDay(date, 0.01)), None,
             "missing prior benchmark NAV"),
        )
        for benchmark, prior_nav, message in cases:
            with self.subTest(message=message):
                account = _account("adjusted_return", 1_000, [])
                opening = OpenSnapshot(date, account, pd.DataFrame(columns=VALUE_COLUMNS), 0.0, 1_000)
                cache = _close_inputs(
                    date, account, opening, _execution(date, account), dates=(date,),
                    initial_cash=1_000, benchmark=benchmark,
                )
                if prior_nav is None:
                    cache.values[(Topic.ACCOUNT_INITIAL, None)] = InitialAccount(
                        account, 1_000, 1.0, None,
                    )
                with self.assertRaisesRegex(ValueError, message):
                    _mark_close(
                        PortfolioAccounting(_config(benchmark_mode="csi300")), cache, date,
                        pd.DataFrame(columns=["code"]),
                    )


    def test_holding_without_a_consistent_price_fails(self):
        date = "2024-01-02"
        cases = (
            ("adjusted_return", {"position_value": 100.0},
             {"adjusted_open": None, "is_suspended": True, "is_missing": False}),
            ("raw_price", {"total_shares": 10, "sellable_shares": 10},
             {"adjusted_open": 100.0, "is_suspended": False, "is_missing": False}),
        )
        for mode, holding, market_values in cases:
            with self.subTest(price_mode=mode):
                row = {
                    "code": "A", **holding, "reference_price": None, "reference_date": None,
                }
                account = _account(mode, 0, [row])
                cache = MemoryCache({(Topic.ACCOUNT_SETTLED, date): account})
                with self.assertRaisesRegex(ValueError, "no valid.*price"):
                    PortfolioAccounting(_config(mode)).mark_at_open(
                        date=date, market=pd.DataFrame([{"code": "A", **market_values}]),
                        cache=cache,
                    )

if __name__ == "__main__":
    unittest.main()
