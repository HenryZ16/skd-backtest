"""A-share execution placeholder using the fixed cache and generator protocol."""

from collections.abc import Iterator
import pandas as pd

from .config import BacktestConfig
from .contracts import ExecutionResult, Topic
from .runtime_cache import CacheView
from .schemas import empty_result


class Broker:
    def __init__(self, config: BacktestConfig):
        self.config = config

    def start_day(self, *, date: str, cache: CacheView) -> None:
        # TODO: Release eligible share locks on each trading day.
        cache.log(level="DEBUG", message="STUB settlement: account unchanged")
        cache.publish(Topic.ACCOUNT_SETTLED, date, cache.read(Topic.ACCOUNT_ACTIONS, date).account)

    def execute(self, *, date: str, market: pd.DataFrame, cache: CacheView) -> Iterator[int]:
        account = cache.read(Topic.ACCOUNT_OPEN, date).account
        signal_date = (cache.read(Topic.SIGNAL_TARGETS, date).signal_date
                       if cache.contains(Topic.SIGNAL_TARGETS, date) else None)
        # TODO: Enforce tradability, sell before buy, request fees, and update actual cash/holdings.
        cache.log(level="DEBUG", message="STUB execution: no orders or trades")
        cache.publish(Topic.EXECUTION_DAY, date,
                      ExecutionResult(date, account, empty_result("orders"), empty_result("trades"),
                                      0.0, 0.0, signal_date))
        yield from ()  # No orders means no fee requests; retain the agreed generator interface.
