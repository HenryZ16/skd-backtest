"""Focused checks for date/code alignment and cross-sectional RankIC."""

import math
import unittest

import pandas as pd

from skd_backtest.contracts import PredictionResult, Topic
from skd_backtest.prediction_evaluator import PredictionEvaluator
from skd_backtest.schemas import RESULT_COLUMNS


class EvaluatorCache:
    def __init__(self, scores, labels):
        self.scores = scores
        self.labels = labels
        self.published = {}

    def history(self, topic):
        assert topic == Topic.SIGNAL_SCORES
        return self.scores

    def read(self, topic):
        assert topic == Topic.EVALUATION_LABELS
        return self.labels

    def publish(self, topic, key, value):
        self.published[topic, key] = value


def evaluate(scores, labels):
    cache = EvaluatorCache(scores, labels)
    PredictionEvaluator().evaluate(cache=cache)
    return cache


class PredictionEvaluatorTest(unittest.TestCase):
    def test_aligns_shuffled_labels_keeps_missing_scores_and_uses_tied_average_ranks(self):
        scores = pd.DataFrame(
            [
                ("2024-01-04", "C", math.inf),
                ("2024-01-03", "D", 4.0),
                ("2024-01-04", "A", 2.0),
                ("2024-01-03", "A", 1.0),
                ("2024-01-04", "B", math.nan),
                ("2024-01-03", "B", 1.0),
                ("2024-01-03", "C", 3.0),
                ("2024-01-04", "D", 8.0),
            ],
            columns=("date", "code", "score"),
        )
        labels = pd.DataFrame(
            [
                ("2024-01-04", "C", 0.3),
                ("2024-01-03", "D", 0.4),
                ("2024-01-03", "B", 0.3),
                ("2024-01-04", "A", math.nan),
                ("2024-01-03", "A", 0.1),
                ("2024-01-03", "C", 0.2),
                ("2024-01-04", "B", math.inf),
            ],
            columns=("date", "code", "future_return"),
        )
        original_scores = scores.copy(deep=True)
        original_labels = labels.copy(deep=True)

        cache = evaluate(scores, labels)
        result = cache.published[Topic.EVALUATION_PREDICTION, None]

        self.assertIsInstance(result, PredictionResult)
        self.assertEqual(tuple(result.predictions.columns), RESULT_COLUMNS["predictions"])
        self.assertEqual(
            list(result.predictions[["date", "code"]].itertuples(index=False, name=None)),
            [
                ("2024-01-03", "A"),
                ("2024-01-03", "B"),
                ("2024-01-03", "C"),
                ("2024-01-03", "D"),
                ("2024-01-04", "A"),
                ("2024-01-04", "B"),
                ("2024-01-04", "C"),
                ("2024-01-04", "D"),
            ],
        )
        self.assertEqual(result.predictions.loc[:3, "future_return"].tolist(), [0.1, 0.3, 0.2, 0.4])
        self.assertTrue(pd.isna(result.predictions.loc[4, "future_return"]))
        self.assertTrue(math.isinf(result.predictions.loc[5, "future_return"]))
        self.assertEqual(result.predictions.loc[6, "future_return"], 0.3)
        self.assertTrue(pd.isna(result.predictions.loc[7, "future_return"]))
        self.assertEqual(tuple(result.rankic.columns), RESULT_COLUMNS["rankic"])
        self.assertEqual(result.rankic.date.tolist(), ["2024-01-03", "2024-01-04"])
        self.assertEqual(result.rankic.n_stocks.tolist(), [4, 0])
        self.assertAlmostEqual(result.rankic.loc[0, "rankic"], 3 / math.sqrt(22.5))
        self.assertIsNone(result.rankic.loc[1, "rankic"])
        pd.testing.assert_frame_equal(scores, original_scores)
        pd.testing.assert_frame_equal(labels, original_labels)

    def test_single_pair_and_zero_rank_variance_return_none(self):
        scores = pd.DataFrame(
            [
                ("2024-02-01", "A", 1.0),
                ("2024-02-02", "A", 1.0),
                ("2024-02-02", "B", 1.0),
                ("2024-02-03", "A", 1.0),
                ("2024-02-03", "B", 2.0),
            ],
            columns=("date", "code", "score"),
        )
        labels = pd.DataFrame(
            [
                ("2024-02-01", "A", 0.1),
                ("2024-02-02", "A", 0.1),
                ("2024-02-02", "B", 0.2),
                ("2024-02-03", "A", 0.2),
                ("2024-02-03", "B", 0.2),
            ],
            columns=("date", "code", "future_return"),
        )

        result = evaluate(scores, labels).published[Topic.EVALUATION_PREDICTION, None]

        self.assertEqual(result.rankic.n_stocks.tolist(), [1, 2, 2])
        self.assertTrue(result.rankic.rankic.isna().all())

    def test_empty_scores_publish_exact_empty_schemas(self):
        scores = pd.DataFrame(columns=("date", "code", "score"))
        labels = pd.DataFrame(columns=("date", "code", "future_return"))
        original_scores = scores.copy(deep=True)
        original_labels = labels.copy(deep=True)

        result = evaluate(scores, labels).published[Topic.EVALUATION_PREDICTION, None]

        self.assertTrue(result.predictions.empty)
        self.assertEqual(tuple(result.predictions.columns), RESULT_COLUMNS["predictions"])
        self.assertTrue(result.rankic.empty)
        self.assertEqual(tuple(result.rankic.columns), RESULT_COLUMNS["rankic"])
        pd.testing.assert_frame_equal(scores, original_scores)
        pd.testing.assert_frame_equal(labels, original_labels)


if __name__ == "__main__":
    unittest.main()
