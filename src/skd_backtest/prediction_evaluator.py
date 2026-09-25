"""Prediction-evaluation placeholder; retain scores without calculated labels."""

from .contracts import PredictionResult, Topic
from .runtime_cache import CacheView
from .schemas import empty_result


class PredictionEvaluator:
    def evaluate(self, *, cache: CacheView) -> None:
        cache.read(Topic.EVALUATION_LABELS)
        scores = cache.history(Topic.SIGNAL_SCORES)
        # TODO: Align labels by date/code and calculate each signal day's Spearman RankIC.
        cache.log(level="DEBUG", message="STUB prediction evaluation: RankIC not calculated")
        cache.publish(Topic.EVALUATION_PREDICTION, None,
                      PredictionResult(scores.assign(future_return=None), empty_result("rankic")))
