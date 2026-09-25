"""Corporate-action placeholder; preserve the account until postings are implemented."""

import pandas as pd

from .config import BacktestConfig
from .contracts import ActionResult, Topic
from .runtime_cache import CacheView
from .schemas import EVENT_COLUMNS


class CorporateActionEngine:
    def __init__(self, config: BacktestConfig):
        self.config = config

    def apply(self, *, date: str, cache: CacheView) -> None:
        dates = cache.read(Topic.RUN_CALENDAR).trading_dates
        index = dates.index(date)
        account = (cache.read(Topic.ACCOUNT_CLOSE, dates[index - 1]).account if index
                   else cache.read(Topic.ACCOUNT_INITIAL).account)
        if self.config.price_mode == "raw_price":
            cache.read(Topic.REFERENCE_ACTIONS, date)
            # TODO: Apply raw-price events using record-date holdings and event deduplication.
            cache.log(level="DEBUG", message="STUB corporate actions: account unchanged")
        cache.publish(Topic.ACCOUNT_ACTIONS, date,
                      ActionResult(date, account, pd.DataFrame(columns=EVENT_COLUMNS)))
