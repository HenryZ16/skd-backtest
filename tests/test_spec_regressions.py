"""Regressions for the independent full-specification audit."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import random
import sys
import unittest

import numpy as np
import pandas as pd

from skd_backtest import DataCapabilities, OptimizerConfig, ReferenceSources
from skd_backtest.data_provider import _price_history
from skd_backtest.evaluate import main
from skd_backtest.submission_runner import SubmissionRunner
from skd_backtest.schemas import SOURCE_COLUMNS, RESULT_COLUMNS
import test_financial_integration as fixtures


MODEL = '''
import random
from pathlib import Path
import numpy as np
import pandas as pd
from .helper import OFFSET
MODULE_RANDOM = random.random() + np.random.random()

class InferenceModel:
    initializations = 0
    def __init__(self, model_dir):
        type(self).initializations += 1
        files = sorted(Path(model_dir).iterdir())
        self.bias = sum(float(path.read_text()) for path in files)
        self.bias += OFFSET + MODULE_RANDOM + random.random() + np.random.random()
        self.calls = 0
        self.dates = []
    def predict(self, as_of_date, data):
        self.calls += 1
        self.dates.append(as_of_date)
        barra = data["Barra_factor"]
        codes = sorted(barra.loc[barra.iloc[:, 0] == int(as_of_date.replace("-", ""))].iloc[:, 1])
        return pd.DataFrame({
            "date": as_of_date, "code": codes,
            "score": [self.bias + self.calls + random.random() + np.random.random() for _ in codes],
        })
'''


class SpecRegressionTests(unittest.TestCase):
    setUp = fixtures.FinancialIntegrationTest.setUp
    engine = fixtures.FinancialIntegrationTest.engine
    inference = fixtures.FinancialIntegrationTest.inference

    def submission(self, name="team", offset=1):
        directory = self.root / name
        (directory / "model").mkdir(parents=True)
        (directory / "model/a.txt").write_text("2", encoding="utf-8")
        (directory / "model/b.txt").write_text("3", encoding="utf-8")
        (directory / "helper.py").write_text(f"OFFSET = {offset}\n", encoding="utf-8")
        (directory / "inference.py").write_text(MODEL, encoding="utf-8")
        return directory

    def test_standard_submission_reinitializes_once_per_run_and_is_reproducible(self):
        directory = self.submission()
        python_before = random.getstate()
        numpy_before = np.random.get_state()
        engine = self.engine(inference=None, submission_dir=directory, random_seed=123)
        model = engine.submission_runner.model
        self.assertEqual(model.initializations, 1)
        engine.run()
        self.assertIs(engine.submission_runner.model, model)
        self.assertEqual(model.calls, 2)
        self.assertEqual(model.dates, ["2018-01-02", "2018-01-04"])
        tables, metrics, account = engine.tables, engine.metrics, engine.account
        engine.run()
        self.assertIsNot(engine.submission_runner.model, model)
        self.assertEqual(engine.submission_runner.model.initializations, 1)
        for name in RESULT_COLUMNS:
            pd.testing.assert_frame_equal(tables[name], engine.tables[name])
        self.assertEqual(metrics, engine.metrics)
        self.assertEqual(account, engine.account)
        for prefetch, asynchronous in ((False, False), (False, True), (True, False)):
            other = self.engine(inference=None, submission_dir=directory, random_seed=123,
                                prefetch=prefetch, async_inference=asynchronous)
            other.run()
            self.assertEqual(metrics, other.metrics)
            for name in RESULT_COLUMNS:
                pd.testing.assert_frame_equal(tables[name], other.tables[name])
        self.assertEqual(python_before, random.getstate())
        np.testing.assert_equal(numpy_before, np.random.get_state())
        different = self.engine(inference=None, submission_dir=directory, random_seed=124)
        different.run()
        self.assertFalse(tables["predictions"].score.equals(different.tables["predictions"].score))

    def test_submission_relative_imports_and_failed_loading_are_isolated(self):
        first = SubmissionRunner.from_submission(self.submission("one", 1), random_seed=7)
        second = SubmissionRunner.from_submission(self.submission("two", 10), random_seed=7)
        self.assertAlmostEqual(second.model.bias - first.model.bias, 9)
        broken = self.submission("broken")
        (broken / "inference.py").write_text("raise RuntimeError('bad model')\n", encoding="utf-8")
        before = {name for name in sys.modules if name.startswith("_skd_submission_")}
        with self.assertRaisesRegex(RuntimeError, "bad model"):
            SubmissionRunner.from_submission(broken)
        self.assertEqual(before, {name for name in sys.modules if name.startswith("_skd_submission_")})

    def test_cli_loads_shared_config_and_writes_required_outputs(self):
        directory = self.submission()
        config = {
            "backtest": {"data_dir": ".", "start_date": "2018-01-02", "end_date": "2018-01-05",
                         "output_dir": "cli-output", "initial_cash": 1000, "lookback": 1,
                         "holding_period": 1, "rebalance_interval": 2, "random_seed": 42},
            "optimizer": {"top_k": 1},
        }
        path = self.root / "config.json"
        for setting, flag, friendly in (
            (None, None, True), (False, None, False),
            (True, "--no-friendly-output", False), (False, "--friendly-output", True),
        ):
            folder = self.root / f"cli-output-{setting}-{flag}"
            config["backtest"]["output_dir"] = folder.name
            if setting is not None:
                config["backtest"]["friendly_output"] = setting
            path.write_text(json.dumps(config), encoding="utf-8")
            argv = ["--submission", str(directory), "--config", str(path)]
            with (self.subTest(setting=setting, flag=flag), redirect_stdout(StringIO()) as output,
                  redirect_stderr(StringIO()) as progress):
                self.assertEqual(main(argv + ([flag] if flag else [])), 0)
            metrics = json.loads((folder / "metrics.json").read_text())
            if friendly:
                self.assertEqual(output.getvalue().count("回测完成"), 1)
                self.assertIn("平均 RankIC", output.getvalue())
                self.assertNotIn('"total_return":', output.getvalue())
                self.assertIn("100.0%", progress.getvalue())
            else:
                self.assertEqual(json.loads(output.getvalue()), metrics)
                self.assertEqual(progress.getvalue(), "")
            self.assertEqual(len(list(folder.iterdir())), 9)

    def test_suspended_finite_close_never_enters_history_or_seed_in_either_mode(self):
        for mode in ("adjusted_return", "raw_price"):
            capabilities = DataCapabilities(raw_prices=True, price_limits=True)
            engine = self.engine(price_mode=mode, data_capabilities=capabilities)
            provider = engine.data_provider
            column = "close" if mode == "adjusted_return" else "raw_close"
            frame = pd.DataFrame({
                "date": [20180102, 20180103, 20180104],
                "code": ["A"] * 3, column: [10.0, 99.0, np.nan], "is_suspend": [False, True, True],
            }).rename(columns={"date": SOURCE_COLUMNS["MarketData"][0],
                              "code": SOURCE_COLUMNS["MarketData"][1]})
            previous = pd.DataFrame(columns=["reference_close", "reference_date"])
            together, _ = _price_history(frame, [20180102, 20180103, 20180104], previous, column)
            self.assertEqual(together.reference_close.tolist(), [10.0] * 3)
            self.assertEqual(together.has_valid_close.tolist(), [True, False, False])
            first, state = _price_history(frame.iloc[:2], [20180102, 20180103], previous, column)
            next_batch, _ = _price_history(frame.iloc[2:], [20180104], state, column)
            self.assertEqual(next_batch.reference_close.tolist(), [10.0])
            self.assertEqual(next_batch.reference_date.tolist(), [20180102])
            path = self.root / f"{mode}_seed.parquet"
            frame.to_parquet(path, index=False)
            provider._first_date = 20180201
            seed = provider._read_seed_prices(path)
            self.assertEqual(seed.reference_close.tolist(), [10.0])

    def test_suspension_does_not_change_nav_via_next_day_reference(self):
        path = self.root / "MarketData/2018/01/201801.parquet"
        frame = pd.read_parquet(path)
        date_column, code_column = SOURCE_COLUMNS["MarketData"][:2]
        mask = frame[code_column].eq("SH600000")
        frame.loc[mask & frame[date_column].eq(20180104), ["close", "is_suspend"]] = [99.0, True]
        frame.loc[mask & frame[date_column].eq(20180105), ["close", "is_suspend"]] = [np.nan, True]
        for field in ("open", "high", "low", "close"):
            frame["raw_" + field] = frame[field]
        frame["upper_limit"], frame["lower_limit"] = 1000.0, .01
        frame.to_parquet(path, index=False)
        def inference(as_of_date, data):
            return pd.DataFrame({"date": as_of_date, "code": ["SH600000", "SZ000001"], "score": [2., 1.]})
        for mode in ("adjusted_return", "raw_price"):
            engine = self.engine(inference=inference, price_mode=mode,
                                 data_capabilities=DataCapabilities(raw_prices=True, price_limits=True))
            engine.run()
            self.assertEqual(engine.tables["equity_curve"].portfolio_value.tolist(),
                             [1000.0, 1100.0, 1100.0, 1100.0])

    def test_barra_optimizer_runs_through_existing_financial_components(self):
        dates = ["2018-01-02", "2018-01-03", "2018-01-04", "2018-01-05"]
        codes = ["SH600000", "SZ000001"]
        factor = SOURCE_COLUMNS["Barra_factor"][3]
        tables = {
            "benchmark_returns": pd.DataFrame({"date": dates, "benchmark_return": [0.] * 4}),
            "benchmark_weights": pd.DataFrame([(day, code, .5) for day in dates for code in codes],
                                             columns=["date", "code", "benchmark_weight"]),
            "industries": pd.DataFrame([(day, code, "industry") for day in dates for code in codes],
                                      columns=["date", "code", "industry"]),
            "factor_covariance": pd.DataFrame([(day, factor, factor, .1) for day in dates],
                                             columns=["date", "factor1", "factor2", "covariance"]),
            "specific_risk": pd.DataFrame([(day, code, 1.) for day in dates for code in codes],
                                         columns=["date", "code", "specific_variance"]),
        }
        sources = {}
        for name, table in tables.items():
            sources[name] = self.root / (name + ".parquet")
            table.to_parquet(sources[name], index=False)
        engine = self.engine(
            benchmark_mode="csi300",
            data_capabilities=DataCapabilities(benchmark_returns=True, benchmark_weights=True,
                industries=True, factor_covariance=True, specific_risk=True),
            reference_sources=ReferenceSources(**sources),
            optimizer_config=OptimizerConfig(method="barra", barra_factors=(factor,),
                risk_aversion=2, active_weight_limit=.2, industry_exposure_limit=0.,
                barra_style_exposure_limit=0., turnover_limit=1.),
        )
        engine.run()
        targets = engine.tables["target_weights"]
        np.testing.assert_allclose(targets.target_weight, [.7, .3, .3, .7], atol=1e-7)
        self.assertFalse(engine.tables["trades"].empty)
        self.assertIsNotNone(engine.metrics["total_return"])
        self.assertTrue(engine.tables["equity_curve"].benchmark_nav.eq(1).all())
        self.assertFalse(engine.reference_data._tables)
        # The same engine reloads point-in-time risk inputs after each run.
        tables["specific_risk"]["specific_variance"] = 10.
        tables["specific_risk"].to_parquet(sources["specific_risk"], index=False)
        engine.run()
        self.assertLess(engine.tables["target_weights"].iloc[0].target_weight, .7)


if __name__ == "__main__":
    unittest.main()
