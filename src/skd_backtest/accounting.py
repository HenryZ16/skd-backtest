"""Daily valuation, separate from desired allocations and trading decisions."""

import logging

import pandas as pd

from .schemas import empty_result


logger = logging.getLogger(__name__)


class PortfolioAccounting:
    def mark_at_open(self, *, date: str, account: dict, market: pd.DataFrame, price_mode: str) -> None:
        """Refresh pre-trade equity in place using opening information only (stub)."""
        # TODO: 公司行为及日初处理后，以开盘时可获得价格更新实际持仓市值和账户权益。
        # 无有效开盘价时保留持仓，采用历史参考价；不能读取当日收盘价或沿用目标权重。
        # raw_price 使用真实价格；adjusted_return 按实际权重和复权收益推进，不能虚构真实股数。
        # 此处不生成收盘审计行，也不能覆盖计算日收益所需的上一日收盘账户价值。
        logger.debug("[PortfolioAccounting.mark_at_open] STUB date=%s, mode=%s", date, price_mode)

    def current_weights(self, account: dict) -> pd.DataFrame:
        """Read actual weights after the current day's close valuation (stub)."""
        # TODO: 从真实持仓市值和账户净值计算当期权重，包含无法卖出的调出成分。
        logger.debug("[PortfolioAccounting.current_weights] STUB actual holdings")
        return pd.DataFrame(columns=["code", "weight"])

    def mark_to_market(self, *, date: str, account: dict,
                       market: pd.DataFrame, price_mode: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Update account in place and return daily positions/equity audit rows (stub)."""
        # TODO: 每个交易日估值，包括非调仓日；停牌持仓保留并沿用最近有效价格。
        # raw_price: market_value = sum(shares * raw_close), equity = cash + market_value。
        # adjusted_return: 由实际权重及复权收益计算净值，不能再入账公司行为。
        # 更新组合/基准 NAV、日收益、active_return、换手、成本，并记录持仓。
        logger.debug("[PortfolioAccounting.mark_to_market] STUB date=%s, mode=%s", date, price_mode)
        return empty_result("positions"), empty_result("equity_curve")
