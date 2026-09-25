"""All raw metrics remain None until their financial calculations are implemented."""

from .contracts import Topic
from .runtime_cache import CacheView
from .schemas import METRIC_NAMES


class Metrics:
    def __init__(self, trading_days_per_year: int, risk_free_rate: float):
        self.trading_days_per_year = trading_days_per_year
        self.risk_free_rate = risk_free_rate

    def calculate(self, *, cache: CacheView) -> None:
        # TODO: Calculate the 15 raw metrics from audit tables; do not add leaderboard weights.
        cache.log(level="DEBUG", message="STUB metrics: all 15 values are None (not calculated)")
        cache.publish(Topic.EVALUATION_METRICS, None, dict.fromkeys(METRIC_NAMES))
