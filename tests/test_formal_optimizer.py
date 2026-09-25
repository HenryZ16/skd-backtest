"""Analytic and end-to-end requirements for the formal risk optimizer."""
from dataclasses import replace
import unittest

import numpy as np
import pandas as pd

from skd_backtest.config import OptimizerConfig
from skd_backtest.contracts import Dataset, Topic
from skd_backtest.portfolio_optimizer import PortfolioOptimizer
from skd_backtest.risk_optimizer import asset_covariance
from test_component_optimizer import make_cache


def risk_cache(scores=None, *, current=None):
    cache, dates = make_cache(
        scores or {"A": 2.0, "B": 1.0}, codes=("A", "B"), held=(),
        benchmark_weights={"A": 0.5, "B": 0.5}, benchmark_mode="csi300")
    date = dates[1]
    inputs = cache.packets[Topic.REFERENCE_PORTFOLIO, date]
    inputs = replace(
        inputs,
        barra_exposures=Dataset("available", pd.DataFrame({
            "date": [date, date], "code": ["A", "B"], "factor": [1.0, -1.0]})),
        factor_covariance=Dataset("available", pd.DataFrame({
            "date": [date], "factor1": ["factor"], "factor2": ["factor"], "covariance": [0.0]})),
        specific_risk=Dataset("available", pd.DataFrame({
            "date": [date, date], "code": ["A", "B"], "specific_variance": [1.0, 1.0]})),
        industries=Dataset("available", pd.DataFrame({
            "date": [date, date], "code": ["A", "B"], "industry": ["first", "second"]})),
    )
    cache.packets[Topic.REFERENCE_PORTFOLIO, date] = inputs
    cache.packets[Topic.ACCOUNT_CLOSE, date].actual_weights = pd.DataFrame(
        list((current or {"A": .5, "B": .5}).items()), columns=["code", "weight"])
    return cache, dates


class FormalOptimizerTests(unittest.TestCase):
    def solve(self, *, cache=None, **options):
        if cache is None:
            cache, dates = risk_cache()
        else:
            dates = cache.read(Topic.RUN_CALENDAR).trading_dates
        config = OptimizerConfig(method="barra", risk_aversion=2, barra_factors=("factor",), **options)
        PortfolioOptimizer(config).optimize(signal_date=dates[1], cache=cache)
        return cache.published[Topic.SIGNAL_TARGETS, dates[2]].weights.set_index("code").target_weight

    def test_factor_risk_matrix_matches_hand_calculation(self):
        cache, dates = risk_cache()
        inputs = cache.read(Topic.REFERENCE_PORTFOLIO, dates[1])
        inputs.barra_exposures.data["factor"] = [1.0, 2.0]
        inputs.factor_covariance.data["covariance"] = .25
        inputs.specific_risk.data["specific_variance"] = [.1, .2]
        matrix, _ = asset_covariance(
            config=OptimizerConfig(barra_factors=("factor",)), date=dates[1], codes=["A", "B"], inputs=inputs)
        np.testing.assert_allclose(matrix, [[.35, .5], [.5, 1.2]])

    def test_unconstrained_solution_and_monotonic_scores(self):
        actual = self.solve()
        self.assertAlmostEqual(actual.A, .75, places=7)
        self.assertAlmostEqual(actual.B, .25, places=7)
        cache, _ = risk_cache({"A": 100.0, "B": -40.0})
        pd.testing.assert_series_equal(actual, self.solve(cache=cache))

    def test_weight_industry_style_and_turnover_constraints_bind(self):
        for option, expected in (
            ({"single_name_weight_limit": .55}, .55),
            ({"active_weight_limit": .1}, .6),
            ({"industry_exposure_limit": .05}, .55),
            ({"barra_style_exposure_limit": .1}, .55),
            ({"turnover_limit": .2}, .6),
            ({"active_weight_limit": 0.0}, .5),
        ):
            with self.subTest(option=option):
                result = self.solve(**option)
                self.assertAlmostEqual(result.A, expected, places=7)
                self.assertAlmostEqual(result.sum(), 1.0, places=8)

    def test_joint_constraints_cash_and_exits(self):
        actual = self.solve(active_weight_limit=.1, industry_exposure_limit=.1,
                            barra_style_exposure_limit=.1, turnover_limit=.2)
        self.assertAlmostEqual(actual.A, .55, places=7)
        self.assertGreaterEqual(actual.min(), 0)
        self.assertLessEqual(actual.sum(), 1)
        cash = self.solve(fully_invested=False, single_name_weight_limit=.4)
        self.assertLessEqual(cash.sum(), .8 + 1e-8)
        cache, _ = risk_cache(current={"A": .4, "B": .4, "EXIT": .2})
        exit_weights = self.solve(cache=cache, turnover_limit=.4)
        self.assertEqual(exit_weights.EXIT, 0)
        turnover = abs(exit_weights.A - .4) + abs(exit_weights.B - .4) + .2
        self.assertLessEqual(turnover, .4 + 1e-8)
        cache, _ = risk_cache(current={"A": .4, "B": .4, "EXIT": .2})
        with self.assertRaisesRegex(ValueError, "infeasible"):
            self.solve(cache=cache, turnover_limit=.3)

    def test_infeasible_constraints_and_invalid_risk_never_publish(self):
        for change in ("cap", "missing", "negative", "future"):
            cache, dates = risk_cache()
            inputs = cache.read(Topic.REFERENCE_PORTFOLIO, dates[1])
            options = {}
            if change == "cap":
                options["single_name_weight_limit"] = .4
            elif change == "missing":
                inputs.specific_risk.data.drop(index=1, inplace=True)
            elif change == "negative":
                inputs.factor_covariance.data["covariance"] = -1.0
            else:
                inputs.factor_covariance.data["date"] = dates[2]
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.solve(cache=cache, **options)
            self.assertFalse(cache.published)


if __name__ == "__main__":
    unittest.main()
