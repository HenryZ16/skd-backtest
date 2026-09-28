"""Check actual inference inputs and results at the shared runtime boundary."""
from functools import wraps
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import pandas as pd
from typeguard import TypeCheckError

from skd_backtest import BacktestEngine
from skd_backtest.submission_runner import SubmissionRunner


def predict(as_of_date, data):
    return pd.DataFrame({"date": [as_of_date], "code": ["SH600000"], "score": [1.0]})


class Model:
    static = staticmethod(predict)

    def predict(self, as_of_date, data):
        return predict(as_of_date, data)

    __call__ = predict


class InferenceContractTest(unittest.TestCase):
    def infer(self, runner, *, as_of_date="2024-01-02", data=None, check_schema=True):
        return runner.infer(as_of_date=as_of_date, data={} if data is None else data,
                            universe={"SH600000"}, check_schema=check_schema)

    def test_construction_does_not_invoke_inference_or_read_market_data(self):
        inference = Mock(side_effect=AssertionError("must not invoke"))
        with patch("pandas.read_parquet", side_effect=AssertionError("must not read")):
            engine = BacktestEngine(data_dir="missing-data-is-not-read", start_date="2024-01-02",
                                    end_date="2024-01-03", inference=inference)
        self.assertIs(engine.submission_runner.inference, inference)
        inference.assert_not_called()

    def test_plain_functions_methods_and_callable_instances_need_no_annotations(self):
        @wraps(predict)
        def decorated(*args, **kwargs):
            return predict(*args, **kwargs)
        model = Model()
        data = {"Barra_factor": pd.DataFrame(), "Factor33_winsor": pd.DataFrame()}
        for inference in (predict, model.static, model.predict, model, decorated):
            with self.subTest(inference=inference):
                actual = self.infer(SubmissionRunner(inference), data=data)
                pd.testing.assert_frame_equal(actual, predict("2024-01-02", data))

    def test_wrong_date_types_fail_before_calling_model(self):
        inference = Mock(side_effect=AssertionError("must not invoke"))
        runner = SubmissionRunner(inference)
        for date in (20240102, None, pd.Timestamp("2024-01-02")):
            with self.subTest(date=date), self.assertRaisesRegex(TypeCheckError, "as_of_date"):
                self.infer(runner, as_of_date=date)
        inference.assert_not_called()

    def test_data_type_and_every_dictionary_entry_are_checked_before_model_call(self):
        inference = Mock(side_effect=AssertionError("must not invoke"))
        runner = SubmissionRunner(inference)
        frame = pd.DataFrame()
        for data in (None, [], {"valid": frame, 1: frame},
                     {"valid": frame, "invalid": []}, {"valid": frame, "invalid": None}):
            with self.subTest(data=data), self.assertRaisesRegex(TypeCheckError, "data"):
                runner.infer(as_of_date="2024-01-02", data=data,
                             universe={"SH600000"}, check_schema=True)
        inference.assert_not_called()

    def test_wrong_return_type_fails_on_first_and_later_signals(self):
        valid = predict("2024-01-02", {})
        for invalid in (None, [], {}, pd.Series([1.0]), "scores"):
            for check_schema in (True, False):
                results = iter((invalid,) if check_schema else (valid, invalid))
                def inference(as_of_date, data):
                    return next(results)
                runner = SubmissionRunner(inference)
                if not check_schema:
                    self.infer(runner)
                with self.subTest(result=type(invalid), check_schema=check_schema):
                    with self.assertRaisesRegex(TypeCheckError, "DataFrame"):
                        self.infer(runner, check_schema=check_schema)

    def test_standard_submission_uses_same_checks_without_annotations_or_inheritance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model").mkdir()
            (root / "inference.py").write_text(
                "import pandas as pd\nclass InferenceModel:\n"
                "    def __init__(self, model_dir): self.calls = 0\n"
                "    def predict(self, as_of_date, data):\n"
                "        self.calls += 1\n"
                "        if self.calls > 1: return []\n"
                "        return pd.DataFrame({'date': [as_of_date], 'code': ['SH600000'], 'score': [1.0]})\n",
                encoding="utf-8")
            runner = SubmissionRunner.from_submission(root)
            self.assertEqual(runner.model.calls, 0)
            pd.testing.assert_frame_equal(self.infer(runner), predict("2024-01-02", {}))
            with self.assertRaisesRegex(TypeCheckError, "DataFrame"):
                self.infer(runner, check_schema=False)
            self.assertEqual(runner.model.calls, 2)

    def test_incompatible_keywords_raise_python_type_error_when_called(self):
        def inference(date, data):
            raise AssertionError("must not invoke")
        runner = SubmissionRunner(inference)
        with self.assertRaisesRegex(TypeError, "as_of_date"):
            self.infer(runner)


if __name__ == "__main__":
    unittest.main()
