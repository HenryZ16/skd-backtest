"""Evaluate predictions against independently produced forward labels."""

import math

import pandas as pd

from .contracts import PredictionResult, Topic
from .runtime_cache import CacheView
from .schemas import RESULT_COLUMNS


class PredictionEvaluator:
    def evaluate(self, *, cache: CacheView) -> None:
        scores = cache.history(Topic.SIGNAL_SCORES)
        labels = cache.read(Topic.EVALUATION_LABELS)
        predictions = scores.loc[:, ["date", "code", "score"]].merge(
            labels.loc[:, ["date", "code", "future_return"]],
            on=["date", "code"],
            how="left",
            validate="many_to_one",
            sort=False,
        ).sort_values(["date", "code"], kind="stable")
        rankic_rows = []

        for date, day in predictions.groupby("date", sort=True):
            score = pd.to_numeric(day["score"], errors="coerce")
            future_return = pd.to_numeric(day["future_return"], errors="coerce")
            finite = score.map(math.isfinite) & future_return.map(math.isfinite)
            score_rank = score.loc[finite].rank(method="average")
            return_rank = future_return.loc[finite].rank(method="average")
            n_stocks = len(score_rank)
            rankic = (
                score_rank.corr(return_rank)
                if n_stocks >= 2 and score_rank.nunique() > 1 and return_rank.nunique() > 1
                else None
            )
            rankic_rows.append((date, rankic, n_stocks))

        predictions = predictions.loc[:, RESULT_COLUMNS["predictions"]].reset_index(drop=True)
        rankic = pd.DataFrame(rankic_rows, columns=RESULT_COLUMNS["rankic"])
        if rankic_rows:
            rankic["rankic"] = pd.Series([row[1] for row in rankic_rows], dtype=object)
        cache.publish(
            Topic.EVALUATION_PREDICTION,
            None,
            PredictionResult(predictions, rankic),
        )
