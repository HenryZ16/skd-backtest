"""Score-to-weight placeholder, independent of execution and accounting."""

from .config import OptimizerConfig
from .contracts import TargetPlan, Topic
from .runtime_cache import CacheView
from .schemas import empty_result


class PortfolioOptimizer:
    def __init__(self, config: OptimizerConfig):
        self.config = config

    def optimize(self, *, signal_date: str, cache: CacheView) -> None:
        cache.read(Topic.SIGNAL_SCORES, signal_date)
        cache.read(Topic.REFERENCE_PORTFOLIO, signal_date)
        cache.read(Topic.ACCOUNT_CLOSE, signal_date)
        calendar = cache.read(Topic.RUN_CALENDAR).signal_calendar.set_index("signal_date")
        execution_date = calendar.at[signal_date, "execution_date"]
        # TODO: Rank scores and construct Top-K/tilt or constrained target weights.
        cache.log(level="DEBUG", message="STUB optimization: empty target weights")
        cache.publish(Topic.SIGNAL_TARGETS, execution_date,
                      TargetPlan(signal_date, execution_date, empty_result("target_weights")))
