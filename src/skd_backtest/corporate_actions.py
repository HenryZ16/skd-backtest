"""Account for corporate actions once, in the appropriate price system."""

import logging

import pandas as pd


logger = logging.getLogger(__name__)


class CorporateActionEngine:
    def apply(self, *, date: str, actions: pd.DataFrame,
              account: dict, price_mode: str) -> None:
        # TODO: raw_price 模式中分红加现金、送转/拆并股调股数，配股按统一政策处理。
        # adjusted_return 模式不额外入账，避免与复权收益重复计算。
        logger.debug("[CorporateActionEngine.apply] STUB date=%s, mode=%s; no postings", date, price_mode)
