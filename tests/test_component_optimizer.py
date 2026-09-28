import unittest

import pandas as pd

from skd_backtest.config import (
    BacktestConfig,
    CostConfig,
    OptimizerConfig,
)
from skd_backtest.contracts import (
    Dataset,
    PortfolioInputs,
    RunCalendar,
    RunContext,
    TargetPlan,
    Topic,
)
from skd_backtest.portfolio_optimizer import PortfolioOptimizer
from skd_backtest.schemas import WEIGHT_COLUMNS


class FakeCache:
    def __init__(self, packets):
        self.packets = packets
        self.published = {}
        self.logs = []

    def read(self, topic, key=None):
        return self.packets[topic, key]

    def publish(self, topic, key, value):
        self.published[topic, key] = value

    def log(self, **record):
        self.logs.append(record)


def make_cache(
    scores,
    *,
    codes=("A", "B", "C"),
    held=("OLD",),
    benchmark_weights=None,
    benchmark_mode="none",
    signal_date="2024-01-03",
):
    dates = ("2024-01-02", signal_date, "2024-01-04")
    calendar = RunCalendar.from_dates(dates, 1)
    benchmark = (
        Dataset("unavailable", None, "no benchmark source")
        if benchmark_weights is None
        else Dataset(
            "available",
            pd.DataFrame(
                [(signal_date, code, weight) for code, weight in benchmark_weights.items()],
                columns=["date", "code", "benchmark_weight"],
            ),
        )
    )
    inputs = PortfolioInputs(
        signal_date,
        pd.DataFrame({"code": list(codes)}),
        Dataset("available", pd.DataFrame(columns=["date", "code"])),
        benchmark,
        Dataset("unavailable", None, "industry source not configured"),
    )
    closing = type("Closing", (), {
        "actual_weights": pd.DataFrame(
            [(code, 0.1) for code in held], columns=WEIGHT_COLUMNS
        )
    })()
    backtest = BacktestConfig(
        data_dir=".",
        start_date=dates[0],
        end_date=dates[-1],
        initial_cash=100.0,
        rebalance_interval=1,
        holding_period=1,
        lookback=1,
        price_mode="adjusted_return",
        trading_days_per_year=252,
        risk_free_rate=0.0,
        output_dir=None,
        benchmark_mode=benchmark_mode,

    )
    packets = {
        (Topic.SIGNAL_SCORES, signal_date): pd.DataFrame(
            [(signal_date, code, score) for code, score in scores.items()],
            columns=["date", "code", "score"],
        ),
        (Topic.REFERENCE_PORTFOLIO, signal_date): inputs,
        (Topic.ACCOUNT_CLOSE, signal_date): closing,
        (Topic.RUN_CALENDAR, None): calendar,
        (Topic.RUN_CONTEXT, None): RunContext(backtest, OptimizerConfig(), CostConfig()),
    }
    return FakeCache(packets), dates


class PortfolioOptimizerTests(unittest.TestCase):
    def optimize(self, config, scores, **kwargs):
        cache, dates = make_cache(scores, **kwargs)
        PortfolioOptimizer(config).optimize(signal_date=dates[1], cache=cache)
        plan = cache.published[Topic.SIGNAL_TARGETS, dates[2]]
        self.assertIsInstance(plan, TargetPlan)
        return plan, cache, dates

    def test_top_k_manual_weights_ties_scale_and_delisted_holding(self):
        scores = {"A": 10.0, "B": 30.0, "C": 30.0}
        plan, cache, dates = self.optimize(OptimizerConfig(top_k=2), scores)

        self.assertEqual((plan.signal_date, plan.execution_date), (dates[1], dates[2]))
        self.assertEqual(
            list(plan.weights.code), ["A", "B", "C", "OLD"]
        )
        target = plan.weights.set_index("code")
        self.assertEqual(target.loc["A", "target_weight"], 0.0)
        self.assertEqual(target.loc["B", "target_weight"], 0.5)
        self.assertEqual(target.loc["C", "target_weight"], 0.5)
        self.assertTrue(pd.isna(target.loc["OLD", "score"]))
        self.assertEqual(target.loc["OLD", "target_weight"], 0.0)
        self.assertTrue(target.benchmark_weight.isna().all())

        scaled_scores = {code: value * 1000 + 15 for code, value in scores.items()}
        scaled, _, _ = self.optimize(OptimizerConfig(top_k=2), scaled_scores)
        pd.testing.assert_series_equal(
            plan.weights.target_weight,
            scaled.weights.target_weight,
            check_names=False,
        )
        self.assertEqual(cache.published.keys(), {(Topic.SIGNAL_TARGETS, dates[2])})

    def test_tie_break_uses_code_at_top_k_boundary(self):
        plan, _, _ = self.optimize(
            OptimizerConfig(top_k=1),
            {"A": 1.0, "B": 2.0, "C": 2.0},
            held=(),
        )
        target = plan.weights.set_index("code").target_weight
        self.assertEqual(target.to_dict(), {"A": 0.0, "B": 1.0, "C": 0.0})

    def test_top_k_name_cap_with_cash_allowed(self):
        plan, _, _ = self.optimize(
            OptimizerConfig(top_k=2, fully_invested=False, single_name_weight_limit=.35),
            {"A": 1., "B": 2., "C": 3.}, held=(),
        )
        self.assertEqual(plan.weights.set_index("code").target_weight.to_dict(),
                         {"A": 0., "B": .35, "C": .35})

    def test_unavailable_benchmark_weights_are_never_replaced_by_equal_weights(self):
        cache, dates = make_cache(
            {"A": 1.0, "B": 2.0, "C": 3.0},
            benchmark_mode="csi300",
        )
        with self.assertRaisesRegex(ValueError, "benchmark weights are unavailable"):
            PortfolioOptimizer(OptimizerConfig(method="barra")).optimize(
                signal_date=dates[1], cache=cache
            )
        self.assertFalse(cache.published)

    def test_infeasible_cap_and_unsupported_constraints_fail_explicitly(self):
        for config, error in (
            (OptimizerConfig(top_k=2, single_name_weight_limit=0.4), ValueError),
            (OptimizerConfig(long_only=False), NotImplementedError),
            (OptimizerConfig(active_weight_limit=0.1), ValueError),
            (OptimizerConfig(industry_exposure_limit=0.1), ValueError),
            (OptimizerConfig(barra_style_exposure_limit=0.1), ValueError),
            (OptimizerConfig(turnover_limit=0.1), ValueError),
        ):
            with self.subTest(config=config):
                cache, dates = make_cache({"A": 1.0, "B": 2.0, "C": 3.0})
                with self.assertRaises(error):
                    PortfolioOptimizer(config).optimize(
                        signal_date=dates[1], cache=cache
                    )
                self.assertFalse(cache.published)

    def test_empty_pool_does_not_create_a_fake_fully_invested_target(self):
        cache, dates = make_cache(
            {},
            codes=(),
            held=("OLD",),
        )
        with self.assertRaisesRegex(ValueError, "empty legal universe"):
            PortfolioOptimizer(OptimizerConfig()).optimize(
                signal_date=dates[1], cache=cache
            )

        cache, dates = make_cache(
            {},
            codes=(),
            held=("OLD",),
        )
        plan = PortfolioOptimizer(
            OptimizerConfig(fully_invested=False)
        )
        plan.optimize(signal_date=dates[1], cache=cache)
        target = cache.published[Topic.SIGNAL_TARGETS, dates[2]].weights
        self.assertEqual(target.code.tolist(), ["OLD"])
        self.assertEqual(target.target_weight.tolist(), [0.0])


    def test_only_top_k_and_barra_are_supported_without_submodes(self):
        for method in ("top_k", "barra"):
            PortfolioOptimizer(OptimizerConfig(method=method))
        for method in ("benchmark_tilt", "exposure_only", "provided", "unknown"):
            with self.subTest(method=method), self.assertRaises(NotImplementedError):
                PortfolioOptimizer(OptimizerConfig(method=method))
        for option in ("risk_model", "risk_aversion"):
            with self.subTest(option=option), self.assertRaises(TypeError):
                OptimizerConfig(method="barra", **{option: "provided"})


if __name__ == "__main__":
    unittest.main()
