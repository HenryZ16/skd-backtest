"""Focused fee, slippage, and historical schedule checks for CostModel."""

import unittest

from skd_backtest.config import CostConfig, FeeScheduleEntry
from skd_backtest.contracts import CostRequest, Topic
from skd_backtest.cost_model import CostModel


class CostCache:
    def __init__(self, request):
        key = (request.date, request.request_id)
        self.packets = {(Topic.COST_REQUEST, key): request}
        self.logs = []

    def read(self, topic, key=None):
        return self.packets[(topic, key)]

    def publish(self, topic, key, value):
        self.packets[(topic, key)] = value

    def log(self, **record):
        self.logs.append(record)


def make_request(date, side, price_mode, *, base_price=None, shares=None, amount=None):
    return CostRequest(
        1, "order-1", date, side, price_mode, base_price, shares, amount
    )


def quote(config, request):
    cache = CostCache(request)
    CostModel(config).calculate(
        date=request.date, request_id=request.request_id, cache=cache
    )
    return cache.read(Topic.COST_RESULT, (request.date, request.request_id))


class CostModelTest(unittest.TestCase):
    def test_raw_and_adjusted_modes_quote_matching_values_and_fees(self):
        config = CostConfig(
            commission_rate=0.01,
            minimum_commission=2.0,
            slippage=0.1,
            fee_schedule=(
                FeeScheduleEntry("2024-01-01", 0.002, 0.001),
            ),
        )

        for side, factor in (("BUY", 1.1), ("SELL", 0.9)):
            with self.subTest(side=side):
                raw = quote(
                    config,
                    make_request(
                        "2024-03-01", side, "raw_price",
                        base_price=10.0, shares=100,
                    ),
                )
                adjusted = quote(
                    config,
                    make_request(
                        "2024-03-01", side, "adjusted_return", amount=1000.0
                    ),
                )

                self.assertEqual(raw.execution_price, 10.0 * factor)
                self.assertIsNone(adjusted.execution_price)
                self.assertEqual(raw.position_value, 1000.0)
                self.assertEqual(adjusted.position_value, 1000.0)
                self.assertEqual(raw.trade_value, 1000.0 * factor)
                self.assertEqual(adjusted.trade_value, 1000.0 * factor)
                self.assertEqual(raw.commission, adjusted.commission)
                self.assertEqual(raw.stamp_tax, adjusted.stamp_tax)
                self.assertEqual(raw.other_cost, adjusted.other_cost)
                self.assertEqual(raw.total_cost, adjusted.total_cost)
                expected_cash = (
                    -(raw.trade_value + raw.total_cost)
                    if side == "BUY"
                    else raw.trade_value - raw.total_cost
                )
                self.assertEqual(raw.cash_delta, expected_cash)
                self.assertEqual(adjusted.cash_delta, expected_cash)

    def test_minimum_commission_and_side_specific_tax(self):
        config = CostConfig(
            commission_rate=0.001,
            minimum_commission=2.0,
            slippage=0.1,
            fee_schedule=(
                FeeScheduleEntry("2024-01-01", 0.02, 0.03),
            ),
        )
        buy = quote(
            config,
            make_request(
                "2024-05-01", "BUY", "adjusted_return", amount=100.0
            ),
        )
        sell = quote(
            config,
            make_request(
                "2024-05-01", "SELL", "adjusted_return", amount=100.0
            ),
        )

        self.assertAlmostEqual(buy.trade_value, 110.0)
        self.assertAlmostEqual(sell.trade_value, 90.0)
        self.assertEqual(buy.commission, 2.0)
        self.assertEqual(sell.commission, 2.0)
        self.assertEqual(buy.stamp_tax, 0.0)
        self.assertEqual(sell.stamp_tax, 1.8)
        self.assertAlmostEqual(buy.other_cost, 3.3)
        self.assertAlmostEqual(sell.other_cost, 2.7)
        self.assertAlmostEqual(buy.total_cost, 5.3)
        self.assertAlmostEqual(sell.total_cost, 6.5)

    def test_schedule_uses_latest_effective_entry_and_rejects_earlier_dates(self):
        config = CostConfig(fee_schedule=(
            FeeScheduleEntry("2024-01-01", 0.01, 0.002),
            FeeScheduleEntry("2025-01-01", 0.03, 0.004),
        ))

        with self.assertRaisesRegex(ValueError, "no fee schedule entry"):
            quote(
                config,
                make_request(
                    "2023-12-31", "SELL", "adjusted_return", amount=1000.0
                ),
            )

        first_day = quote(
            config,
            make_request("2024-01-01", "SELL", "adjusted_return", amount=1000.0),
        )
        before_change = quote(
            config,
            make_request("2024-12-31", "SELL", "adjusted_return", amount=1000.0),
        )
        change_day = quote(
            config,
            make_request("2025-01-01", "SELL", "adjusted_return", amount=1000.0),
        )

        self.assertEqual((first_day.stamp_tax, first_day.other_cost), (10.0, 2.0))
        self.assertEqual(
            (before_change.stamp_tax, before_change.other_cost), (10.0, 2.0)
        )
        self.assertEqual((change_day.stamp_tax, change_day.other_cost), (30.0, 4.0))

    def test_empty_schedule_defaults_both_tax_rates_to_zero(self):
        result = quote(
            CostConfig(),
            make_request("2024-01-01", "SELL", "adjusted_return", amount=100.0),
        )

        self.assertEqual(result.stamp_tax, 0.0)
        self.assertEqual(result.other_cost, 0.0)
        self.assertEqual(result.total_cost, 0.0)

    def test_invalid_cost_configuration_is_rejected_once_at_model_creation(self):
        invalid_configs = (
            CostConfig(commission_rate=-0.01),
            CostConfig(commission_rate=float("inf")),
            CostConfig(minimum_commission=-1.0),
            CostConfig(minimum_commission=float("nan")),
            CostConfig(slippage=-0.01),
            CostConfig(slippage=1.0),
            CostConfig(slippage=float("inf")),
            CostConfig(fee_schedule=(
                FeeScheduleEntry("2024-01-01", -0.01, 0.0),
            )),
            CostConfig(fee_schedule=(
                FeeScheduleEntry("2024-01-01", 0.0, float("nan")),
            )),
            CostConfig(fee_schedule=(
                FeeScheduleEntry("2024-1-1", 0.0, 0.0),
            )),
            CostConfig(fee_schedule=(
                FeeScheduleEntry("2025-01-01", 0.0, 0.0),
                FeeScheduleEntry("2024-01-01", 0.0, 0.0),
            )),
            CostConfig(fee_schedule=(
                FeeScheduleEntry("2024-01-01", 0.0, 0.0),
                FeeScheduleEntry("2024-01-01", 0.0, 0.0),
            )),
        )
        for config in invalid_configs:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    CostModel(config)

        with self.assertRaises(TypeError):
            CostModel(None)


if __name__ == "__main__":
    unittest.main()
