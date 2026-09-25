"""Call the participant with research data only, then publish validated scores."""

from collections.abc import Callable
from math import isfinite
from numbers import Real

import pandas as pd

from .contracts import Topic
from .runtime_cache import CacheView
from .schemas import SCORE_COLUMNS

Inference = Callable[..., pd.DataFrame]


class SubmissionRunner:
    def __init__(self, inference: Inference):
        self.inference = inference

    def predict(self, *, as_of_date: str, data: dict[str, pd.DataFrame], cache: CacheView) -> None:
        # Capture membership before participant code can mutate its research copy.
        universe = set(cache.read(Topic.REFERENCE_PORTFOLIO, as_of_date).universe.code)
        scores = self.infer(
            as_of_date=as_of_date, data=data, universe=universe,
            check_schema=as_of_date == cache.read(Topic.RUN_CALENDAR).trading_dates[0],
        )
        self.publish_scores(as_of_date=as_of_date, scores=scores, cache=cache)

    def infer(self, *, as_of_date: str, data: dict[str, pd.DataFrame],
              universe: frozenset | set, check_schema: bool) -> pd.DataFrame:
        """Invoke and validate the model without accessing the runtime cache."""
        scores = self.inference(as_of_date=as_of_date, data=data)
        # Schema is fixed for the run; changing row values still need validation each signal.
        if check_schema:
            if not isinstance(scores, pd.DataFrame) or not scores.columns.is_unique or not set(SCORE_COLUMNS).issubset(scores.columns):
                raise ValueError("inference must return a DataFrame with date/code/score")
        scores = scores.loc[:, SCORE_COLUMNS]
        if not scores.date.eq(as_of_date).all():
            raise ValueError("score dates must match as_of_date")
        if scores.code.isna().any() or scores.code.duplicated().any() or set(scores.code) != universe:
            raise ValueError("scores must cover the legal universe exactly once")
        if not all(isinstance(value, Real) and not isinstance(value, bool) and isfinite(value) for value in scores.score):
            raise ValueError("scores must be finite numeric values")
        return scores.sort_values("code", kind="stable", ignore_index=True)

    def publish_scores(self, *, as_of_date: str, scores: pd.DataFrame, cache: CacheView) -> None:
        """Publish only when the owning thread reaches this date's SIGNAL phase."""
        cache.publish(Topic.SIGNAL_SCORES, as_of_date, scores)
