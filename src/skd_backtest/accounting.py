"""Valuation placeholders that carry the empty account through daily snapshots."""

import pandas as pd

from .config import BacktestConfig
from .contracts import CloseSnapshot, OpenSnapshot, Topic
from .runtime_cache import CacheView
from .schemas import RESULT_COLUMNS, VALUE_COLUMNS, WEIGHT_COLUMNS, empty_result


class PortfolioAccounting:
    def __init__(self, config: BacktestConfig):
        self.config = config

    def mark_at_open(self, *, date: str, market: pd.DataFrame, cache: CacheView) -> None:
        account = cache.read(Topic.ACCOUNT_SETTLED, date)
        # TODO: Value actual holdings using only opening information and valid price references.
        cache.log(level="DEBUG", message="STUB open valuation: cash-only snapshot")
        cache.publish(Topic.ACCOUNT_OPEN, date,
                      OpenSnapshot(date, account, pd.DataFrame(columns=VALUE_COLUMNS), 0.0, account.cash))

    def mark_to_market(self, *, date: str, market: pd.DataFrame, cache: CacheView) -> None:
        account = cache.read(Topic.EXECUTION_DAY, date).account
        cache.read(Topic.REFERENCE_BENCHMARK, date)
        # TODO: Value actual holdings and calculate NAV, returns, turnover and costs.
        # The daily row satisfies the handoff contract; uncalculated financial fields stay None.
        row = dict.fromkeys(RESULT_COLUMNS["equity_curve"])
        row.update(date=date, cash=account.cash, market_value=0.0, portfolio_value=account.cash)
        cache.log(level="DEBUG", message="STUB close valuation: NAV and returns not calculated")
        cache.publish(Topic.ACCOUNT_CLOSE, date, CloseSnapshot(
            date, account, empty_result("positions"),
            pd.DataFrame([row], columns=RESULT_COLUMNS["equity_curve"]),
            pd.DataFrame(columns=WEIGHT_COLUMNS),
        ))
