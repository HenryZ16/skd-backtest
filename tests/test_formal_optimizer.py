"""Hand-computed portfolio constraints for the single Barra optimizer."""
from dataclasses import replace
import unittest

import pandas as pd

from skd_backtest.config import OptimizerConfig
from skd_backtest.contracts import Dataset, Topic
from skd_backtest.portfolio_optimizer import PortfolioOptimizer
from test_component_optimizer import make_cache


def risk_cache(scores=None, *, current=None):
    cache, dates = make_cache(
        scores or {"A": 2., "B": 1.}, codes=("A", "B"), held=(),
        benchmark_weights={"A": .5, "B": .5})
    date = dates[1]
    inputs = cache.packets[Topic.REFERENCE_PORTFOLIO, date]
    cache.packets[Topic.REFERENCE_PORTFOLIO, date] = replace(
        inputs,
        barra_exposures=Dataset("available", pd.DataFrame({
            "date": [date, date], "code": ["A", "B"], "名称": ["Alpha", "Beta"], "factor": [1., -1.]})),
        industries=Dataset("available", pd.DataFrame({
            "date": [date, date], "code": ["A", "B"], "industry": ["first", "second"]})),
    )
    cache.packets[Topic.ACCOUNT_CLOSE, date].actual_weights = pd.DataFrame(
        list((current if current is not None else {"A": .5, "B": .5}).items()), columns=["code", "weight"])
    return cache, dates


class FormalOptimizerTests(unittest.TestCase):
    def solve(self, *, cache=None, **options):
        if cache is None:
            cache, dates = risk_cache()
        else:
            dates = cache.read(Topic.RUN_CALENDAR).trading_dates
        config = OptimizerConfig(method="barra", **options)
        PortfolioOptimizer(config).optimize(signal_date=dates[1], cache=cache)
        return cache.published[Topic.SIGNAL_TARGETS, dates[2]].weights.set_index("code").target_weight

    def test_unconstrained_solution_and_monotonic_scores(self):
        actual = self.solve()
        self.assertAlmostEqual(actual.A, 1.)
        self.assertAlmostEqual(actual.B, 0.)
        cache, _ = risk_cache({"A": 100., "B": -40.})
        pd.testing.assert_series_equal(actual, self.solve(cache=cache))

    def test_weight_industry_style_and_turnover_constraints_bind(self):
        for option, expected in (
            ({"single_name_weight_limit": .55}, .55),
            ({"active_weight_limit": .1}, .6),
            ({"industry_exposure_limit": .05}, .55),
            ({"barra_style_exposure_limit": .1}, .55),
            ({"turnover_limit": .2}, .6),
            ({"active_weight_limit": 0.}, .5),
            ({"industry_exposure_limit": 0., "barra_style_exposure_limit": 0.}, .5),
        ):
            with self.subTest(option=option):
                actual = self.solve(**option)
                self.assertAlmostEqual(actual.A, expected, places=7)
                self.assertAlmostEqual(actual.sum(), 1., places=8)

    def test_style_constraints_use_all_data_factors(self):
        for first, second in ((1., 2.), (2., 1.)):
            with self.subTest(first=first, second=second):
                cache, dates = risk_cache()
                inputs = cache.read(Topic.REFERENCE_PORTFOLIO, dates[1])
                factors = inputs.barra_exposures.data.assign(
                    factor=[first, -first], another_factor=[second, -second])
                # Metadata is not an exposure; stock alignment must survive row reordering.
                factors = factors.loc[::-1, ["another_factor", "名称", "date", "factor", "code"]]
                cache.packets[Topic.REFERENCE_PORTFOLIO, dates[1]] = replace(
                    inputs, barra_exposures=Dataset("available", factors))
                actual = self.solve(cache=cache, barra_style_exposure_limit=.1)
                self.assertAlmostEqual(actual.A, .525, places=7)
                self.assertAlmostEqual(actual.B, .475, places=7)
                active = actual.reindex(factors.code).to_numpy() - .5
                for column in ("factor", "another_factor"):
                    self.assertLessEqual(abs(factors[column].to_numpy() @ active), .1 + 1e-8)

    def test_joint_constraints_cash_and_exits(self):
        actual = self.solve(active_weight_limit=.1, industry_exposure_limit=.1,
                            barra_style_exposure_limit=.1, turnover_limit=.2)
        self.assertAlmostEqual(actual.A, .55, places=7)
        self.assertGreaterEqual(actual.min(), 0.)
        self.assertLessEqual(actual.sum(), 1.)
        cash = self.solve(fully_invested=False, single_name_weight_limit=.4)
        self.assertAlmostEqual(cash.A, .4)
        self.assertAlmostEqual(cash.B, 0.)
        cache, _ = risk_cache(current={"A": .4, "B": .4, "EXIT": .2})
        actual = self.solve(cache=cache, turnover_limit=.4)
        self.assertEqual(actual.EXIT, 0.)
        self.assertAlmostEqual(abs(actual.A - .4) + abs(actual.B - .4) + .2, .4)
        cache, _ = risk_cache(current={"A": .4, "B": .4, "EXIT": .2})
        with self.assertRaisesRegex(ValueError, "infeasible"):
            self.solve(cache=cache, turnover_limit=.3)
        self.assertFalse(cache.published)

    def test_empty_account_obeys_initial_turnover_limit(self):
        cache, _ = risk_cache(current={})
        self.assertAlmostEqual(self.solve(cache=cache, turnover_limit=1.).sum(), 1.)
        cache, _ = risk_cache(current={})
        with self.assertRaisesRegex(ValueError, "infeasible"):
            self.solve(cache=cache, turnover_limit=.9)
        self.assertFalse(cache.published)

    def test_invalid_inputs_and_infeasible_constraints_never_publish(self):
        for change in ("cap", "missing", "nonfinite", "empty_factors", "extra_nonfinite", "future", "outside", "industry"):
            cache, dates = risk_cache(current={"A": .5, "B": .5, "EXIT": float("nan")} if change == "outside" else None)
            inputs = cache.read(Topic.REFERENCE_PORTFOLIO, dates[1])
            options = {}
            if change == "cap":
                options["single_name_weight_limit"] = .4
            elif change == "missing":
                inputs.barra_exposures.data.drop(index=1, inplace=True)
            elif change == "nonfinite":
                inputs.barra_exposures.data["factor"] = float("nan")
            elif change == "empty_factors":
                inputs.barra_exposures.data.drop(columns="factor", inplace=True)
            elif change == "extra_nonfinite":
                inputs.barra_exposures.data["another_factor"] = float("nan")
            elif change == "future":
                inputs.barra_exposures.data["date"] = dates[2]
            elif change == "industry":
                inputs.industries.data.drop(index=1, inplace=True)
                options["industry_exposure_limit"] = .1
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.solve(cache=cache, **options)
            self.assertFalse(cache.published)


if __name__ == "__main__":
    unittest.main()
