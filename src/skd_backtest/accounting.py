"""Daily valuation, separate from desired allocations and trading decisions."""

import pandas as pd

from .schemas import empty_result


class PortfolioAccounting:
    def current_weights(self, account: dict) -> pd.DataFrame:
        # TODO: 从真实持仓市值和账户净值计算当期权重，包含无法卖出的调出成分。
        print("[PortfolioAccounting.current_weights] STUB actual holdings")
        return pd.DataFrame(columns=["code", "weight"])

    def mark_to_market(self, *, date: str | None, account: dict,
                       market: pd.DataFrame, price_mode: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        # TODO: 每个交易日估值，包括非调仓日；停牌持仓保留并沿用最近有效价格。
        # raw_price: market_value = sum(shares * raw_close), equity = cash + market_value。
        # adjusted_return: 由实际权重及复权收益计算净值，不能再入账公司行为。
        # 更新组合/基准 NAV、日收益、active_return、换手、成本，并记录持仓。
        print(f"[PortfolioAccounting.mark_to_market] STUB date={date}, mode={price_mode}")
        return empty_result("positions"), empty_result("equity_curve")
