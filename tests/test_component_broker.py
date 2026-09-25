"""Focused order, cash, lot-settlement, and audit checks for Broker."""

from pathlib import Path
from types import SimpleNamespace
import unittest

import pandas as pd

from skd_backtest.broker import Broker
from skd_backtest.config import BacktestConfig
from skd_backtest.contracts import (
    AccountState, CostQuote, CostRequest, ExecutionResult, OpenSnapshot,
    RunCalendar, TargetPlan, Topic,
)
from skd_backtest.schemas import (
    LOCK_COLUMNS, RESULT_COLUMNS, STATE_COLUMNS, VALUE_COLUMNS,
)


DATES = ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08")


def config(price_mode, initial_cash=1_000.0):
    return BacktestConfig(
        data_dir=Path("."), start_date=DATES[0], end_date=DATES[-1],
        initial_cash=initial_cash, rebalance_interval=1, holding_period=1, lookback=1,
        price_mode=price_mode, trading_days_per_year=252, risk_free_rate=0.0,
        output_dir=None,
    )


def plan(signal_date, execution_date, weights):
    rows = [
        (signal_date, execution_date, code, None, None, weight)
        for code, weight in weights
    ]
    return TargetPlan(
        signal_date, execution_date,
        pd.DataFrame(rows, columns=RESULT_COLUMNS["target_weights"]),
    )


def state(price_mode, cash, positions=(), locked_lots=()):
    return AccountState(
        price_mode, cash,
        pd.DataFrame(positions, columns=STATE_COLUMNS[price_mode]),
        pd.DataFrame(locked_lots, columns=LOCK_COLUMNS),
    )


def fixed_quote(request, fee=0.0):
    if request.price_mode == "raw_price":
        execution_price = request.base_price
        position_value = request.shares * request.base_price
        trade_value = request.shares * execution_price
    else:
        execution_price = None
        position_value = request.position_value
        trade_value = position_value
    cash_delta = -(trade_value + fee) if request.side == "BUY" else trade_value - fee
    return CostQuote(
        request.request_id, request.order_id, request.date, request.side, request.price_mode,
        execution_price, position_value, trade_value, fee, 0.0, 0.0, fee, cash_delta,
    )


class ExchangeCache:
    def __init__(self, dates=DATES):
        self.packets = {
            (Topic.RUN_CALENDAR, None): RunCalendar(tuple(dates), pd.DataFrame(
                columns=("signal_date", "execution_date"))),
        }
        self.requests = []
        self.finished = []
        self.logs = []

    def set(self, topic, key, value):
        self.packets[(topic, key)] = value

    def contains(self, topic, key=None):
        return (topic, key) in self.packets

    def read(self, topic, key=None):
        return self.packets[(topic, key)]

    def publish(self, topic, key, value):
        self.packets[(topic, key)] = value
        if topic == Topic.COST_REQUEST:
            self.requests.append(value)

    def finish_quote(self, *, date, request_id):
        key = (date, request_id)
        assert (Topic.COST_REQUEST, key) in self.packets
        assert (Topic.COST_RESULT, key) in self.packets
        del self.packets[Topic.COST_REQUEST, key]
        del self.packets[Topic.COST_RESULT, key]
        self.finished.append(key)

    def log(self, **record):
        self.logs.append(record)


def run_execution(broker, cache, date, market, quote_factory=fixed_quote):
    execution = broker.execute(date=date, market=market, cache=cache)
    while True:
        try:
            request_id = next(execution)
        except StopIteration:
            break
        key = (date, request_id)
        request = cache.read(Topic.COST_REQUEST, key)
        cache.publish(Topic.COST_RESULT, key, quote_factory(request))
    return cache.read(Topic.EXECUTION_DAY, date)


def set_open(cache, date, account, equity, values, targets):
    cache.set(Topic.ACCOUNT_OPEN, date, OpenSnapshot(
        date, account, pd.DataFrame(values, columns=VALUE_COLUMNS),
        sum(row[3] for row in values), equity,
    ))
    cache.set(Topic.SIGNAL_TARGETS, date, targets)


def target_plan(date, weights):
    signal_date = DATES[max(0, DATES.index(date) - 1)]
    return plan(signal_date, date, weights)


class BrokerTest(unittest.TestCase):
    def test_adjusted_amounts_shrink_against_real_cash_and_keep_rejected_holding(self):
        broker = Broker(config("adjusted_return"))
        old_date, date = DATES[0], DATES[1]
        initial = state("adjusted_return", 700.0, [
            ("OLD", 300.0, 10.0, old_date),
        ])
        cache = ExchangeCache()
        targets = target_plan(date, [("OLD", 0.0), ("NEW", 0.7)])
        values = [("OLD", 10.0, date, 300.0)]
        set_open(cache, date, initial, 1_000.0, values, targets)
        market = pd.DataFrame([
            (date, "OLD", 10.0, True, False),
            (date, "NEW", 5.0, False, False),
        ], columns=("date", "code", "adjusted_open", "is_suspended", "is_missing"))

        result = run_execution(
            broker, cache, date, market,
            quote_factory=lambda request: fixed_quote(request, fee=1.0),
        )

        self.assertEqual(result.orders["side"].tolist(), ["SELL", "BUY"])
        self.assertEqual([None if pd.isna(value) else value for value in result.orders["reject_reason"]], ["SUSPENDED", "INSUFFICIENT_CASH"])
        self.assertEqual(result.orders["status"].tolist(), ["REJECTED", "PARTIALLY_FILLED"])
        self.assertTrue(result.trades["shares"].isna().all())
        self.assertTrue(result.trades["price"].isna().all())
        self.assertEqual(result.trades["code"].tolist(), ["NEW"])
        self.assertGreaterEqual(len(cache.requests), 2)
        self.assertEqual([request.request_id for request in cache.requests],
                         list(range(1, len(cache.requests) + 1)))
        self.assertEqual(len({request.order_id for request in cache.requests}), 1)
        self.assertTrue(all(left.position_value > right.position_value
                            for left, right in zip(cache.requests, cache.requests[1:])))
        self.assertEqual(cache.finished, [(date, request.request_id) for request in cache.requests])
        self.assertGreaterEqual(result.account.cash, 0.0)
        self.assertLessEqual(result.account.positions.set_index("code").at["NEW", "position_value"], 699.0)
        self.assertEqual(result.account.positions.set_index("code").at["OLD", "position_value"], 300.0)
        self.assertEqual(initial.positions.iloc[0]["position_value"], 300.0)
        self.assertAlmostEqual(result.total_cost, 1.0)

    def test_raw_limits_are_side_specific_and_missing_open_is_rejected(self):
        broker = Broker(config("raw_price", initial_cash=3_000.0))
        date = DATES[1]
        initial = state("raw_price", 3_000.0, [
            ("DOWN", 100, 100, 10.0, DATES[0]),
            ("UPSELL", 100, 100, 10.0, DATES[0]),
        ])
        cache = ExchangeCache()
        targets = target_plan(date, [
            ("DOWN", 0.0), ("BUYLOW", 0.5), ("MISS", 0.125),
            ("UP", 0.375), ("UPSELL", 0.0),
        ])
        set_open(cache, date, initial, 4_000.0,
                 [("DOWN", 9.0, date, 900.0), ("UPSELL", 10.0, date, 1_000.0)], targets)
        market = pd.DataFrame([
            (date, "DOWN", 9.0, 11.0, 9.0, False, False),
            (date, "BUYLOW", 9.0, 11.0, 9.0, False, False),
            (date, "UP", 10.0, 10.0, 9.0, False, False),
            (date, "UPSELL", 10.0, 10.0, 9.0, False, False),
        ], columns=("date", "code", "raw_open", "upper_limit", "lower_limit",
                    "is_suspended", "is_missing"))

        result = run_execution(broker, cache, date, market)

        self.assertEqual(result.orders[["code", "side"]].values.tolist(), [
            ["DOWN", "SELL"], ["UPSELL", "SELL"], ["BUYLOW", "BUY"],
            ["MISS", "BUY"], ["UP", "BUY"],
        ])
        self.assertEqual([None if pd.isna(value) else value for value in result.orders["reject_reason"]], [
            "LIMIT_DOWN", None, None, "MISSING_OPEN", "LIMIT_UP",
        ])
        self.assertEqual(result.trades[["code", "shares"]].values.tolist(), [
            ["UPSELL", 100], ["BUYLOW", 200],
        ])
        self.assertEqual(result.account.cash, 2_200.0)
        positions = result.account.positions.set_index("code")
        self.assertEqual(positions.at["DOWN", "total_shares"], 100)
        self.assertEqual(positions.at["BUYLOW", "total_shares"], 200)
        self.assertEqual(positions.at["BUYLOW", "sellable_shares"], 0)
        self.assertNotIn("UPSELL", positions.index)
        self.assertEqual(result.account.locked_lots[["code", "shares", "unlock_date"]].values.tolist(),
                         [["BUYLOW", 200, DATES[2]]])
        self.assertEqual(len(cache.requests), 2)
        self.assertEqual(cache.finished, [(date, request_id) for request_id in (1, 2)])

    def test_no_target_day_publishes_empty_result_without_market_scan(self):
        broker = Broker(config("adjusted_return"))
        date = DATES[1]
        account = state("adjusted_return", 25.0, [("A", 75.0, 10.0, DATES[0])])
        cache = ExchangeCache()
        cache.set(Topic.ACCOUNT_OPEN, date, OpenSnapshot(
            date, account, pd.DataFrame(columns=VALUE_COLUMNS), 0.0, 100.0,
        ))

        result = run_execution(broker, cache, date, pd.DataFrame())

        self.assertIs(result.account, account)
        self.assertEqual(tuple(result.orders.columns), RESULT_COLUMNS["orders"])
        self.assertEqual(tuple(result.trades.columns), RESULT_COLUMNS["trades"])
        self.assertTrue(result.orders.empty)
        self.assertTrue(result.trades.empty)
        self.assertEqual((result.trade_value, result.total_cost, result.executed_signal_date),
                         (0.0, 0.0, None))
        self.assertEqual(cache.requests, [])

    def test_raw_tradable_rows_reject_invalid_or_missing_limits(self):
        broker = Broker(config("raw_price", initial_cash=3_000.0))
        date = DATES[1]
        invalid_rows = ((float("nan"), 9.0), (11.0, 0.0), (8.0, 9.0))
        for upper, lower in invalid_rows:
            with self.subTest(upper=upper, lower=lower):
                account = state("raw_price", 3_000.0)
                cache = ExchangeCache()
                set_open(cache, date, account, 3_000.0, [], target_plan(date, [("A", 1.0)]))
                market = pd.DataFrame([
                    (date, "A", 10.0, upper, lower, False, False),
                ], columns=("date", "code", "raw_open", "upper_limit", "lower_limit",
                            "is_suspended", "is_missing"))
                with self.assertRaisesRegex(ValueError, "raw open limits"):
                    run_execution(broker, cache, date, market)

        account = state("raw_price", 3_000.0)
        cache = ExchangeCache()
        set_open(cache, date, account, 3_000.0, [], target_plan(date, [("A", 1.0)]))
        missing_field = pd.DataFrame([
            (date, "A", 10.0, 9.0, False, False),
        ], columns=("date", "code", "raw_open", "lower_limit", "is_suspended", "is_missing"))
        with self.assertRaisesRegex(ValueError, "upper_limit"):
            run_execution(broker, cache, date, missing_field)

    def test_adjusted_buy_uses_quotes_to_find_minimum_fee_boundary(self):
        broker = Broker(config("adjusted_return", initial_cash=5.01))
        date = DATES[1]
        account = state("adjusted_return", 5.01, [
            ("OLD", 94.99, 10.0, DATES[0]),
        ])
        cache = ExchangeCache()
        set_open(cache, date, account, 100.0,
                 [("OLD", 10.0, date, 94.99)],
                 target_plan(date, [("OLD", 0.0), ("NEW", 1.0)]))
        market = pd.DataFrame([
            (date, "OLD", 10.0, True, False),
            (date, "NEW", 10.0, False, False),
        ], columns=("date", "code", "adjusted_open", "is_suspended", "is_missing"))

        result = run_execution(
            broker, cache, date, market,
            quote_factory=lambda request: fixed_quote(request, fee=5.0),
        )

        self.assertEqual(result.orders["status"].tolist(), ["REJECTED", "PARTIALLY_FILLED"])
        self.assertAlmostEqual(result.trades.iloc[0]["position_value"], 0.01, places=9)
        self.assertEqual(result.trades.iloc[0]["total_cost"], 5.0)
        self.assertGreaterEqual(result.account.cash, 0.0)
        self.assertGreaterEqual(len(cache.requests), 3)
        self.assertEqual(cache.finished, [(date, request.request_id) for request in cache.requests])

    def test_adjusted_large_proportional_fee_request_is_not_over_shrunk(self):
        broker = Broker(config("adjusted_return", initial_cash=100.0))
        date = DATES[1]
        account = state("adjusted_return", 100.0, [
            ("OLD", 99_900.0, 10.0, DATES[0]),
        ])
        cache = ExchangeCache()
        set_open(cache, date, account, 100_000.0,
                 [("OLD", 10.0, date, 99_900.0)],
                 target_plan(date, [("OLD", 0.0), ("NEW", 1.0)]))
        market = pd.DataFrame([
            (date, "OLD", 10.0, True, False),
            (date, "NEW", 10.0, False, False),
        ], columns=("date", "code", "adjusted_open", "is_suspended", "is_missing"))

        result = run_execution(
            broker, cache, date, market,
            quote_factory=lambda request: fixed_quote(
                request, fee=request.position_value * 0.001),
        )

        self.assertEqual(result.orders.iloc[-1]["status"], "PARTIALLY_FILLED")
        self.assertAlmostEqual(result.orders.iloc[-1]["filled_value"], 100.0 / 1.001, places=10)
        self.assertEqual(len(cache.requests), 2)
        self.assertGreaterEqual(result.account.cash, 0.0)

    def test_only_due_lots_unlock_and_final_odd_lot_can_be_sold(self):
        broker = Broker(config("raw_price", initial_cash=1.0))
        initial = state("raw_price", 0.0, [
            ("SEC", 257, 150, 10.0, DATES[0]),
        ], [
            ("SEC", 100, DATES[1], "T+1"),
            ("SEC", 7, DATES[3], "pending"),
        ])
        cache = ExchangeCache()
        original = initial

        for date in DATES[1:]:
            cache.set(Topic.ACCOUNT_CLOSE, DATES[DATES.index(date) - 1], SimpleNamespace(account=initial))
            broker.start_day(date=date, cache=cache)
            settled = cache.read(Topic.ACCOUNT_SETTLED, date)
            if date == DATES[1]:
                self.assertEqual(settled.positions.iloc[0]["sellable_shares"], 250)
                self.assertEqual(len(settled.locked_lots), 1)
            if date == DATES[2]:
                self.assertEqual(settled.positions.iloc[0]["sellable_shares"], 0)
                self.assertEqual(settled.locked_lots.iloc[0]["shares"], 7)
            if date == DATES[3]:
                self.assertEqual(settled.positions.iloc[0]["sellable_shares"], 7)
                self.assertTrue(settled.locked_lots.empty)

            initial = settled
            market = pd.DataFrame([
                (date, "SEC", 10.0, 11.0, 9.0, False, False),
            ], columns=("date", "code", "raw_open", "upper_limit", "lower_limit",
                        "is_suspended", "is_missing"))
            market_value = 0.0 if settled.positions.empty else settled.positions.iloc[0]["total_shares"] * 10.0
            set_open(cache, date, settled, 2_570.0,
                     [] if settled.positions.empty else [("SEC", 10.0, date, market_value)],
                     target_plan(date, []))
            result = run_execution(broker, cache, date, market)

            if date == DATES[1]:
                self.assertEqual(result.orders.iloc[0]["status"], "PARTIALLY_FILLED")
                self.assertEqual(result.orders.iloc[0]["reject_reason"], "T1_NOT_SELLABLE")
                self.assertEqual(result.account.positions.iloc[0]["total_shares"], 7)
                self.assertEqual(result.account.cash, 2_500.0)
            elif date == DATES[2]:
                self.assertEqual(result.orders.iloc[0]["status"], "REJECTED")
                self.assertEqual(result.orders.iloc[0]["reject_reason"], "T1_NOT_SELLABLE")
                self.assertEqual(result.account.positions.iloc[0]["total_shares"], 7)
            else:
                self.assertEqual(result.orders.iloc[0]["requested_shares"], 7)
                self.assertEqual(result.orders.iloc[0]["status"], "FILLED")
                self.assertTrue(result.account.positions.empty)
                self.assertEqual(result.account.cash, 2_570.0)

            initial = result.account

        self.assertEqual(original.positions.iloc[0]["sellable_shares"], 150)
        self.assertEqual(len(original.locked_lots), 2)


if __name__ == "__main__":
    unittest.main()
