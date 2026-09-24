"""All raw metrics required by the specification, without invented values."""

import logging

import pandas as pd

METRIC_NAMES = (
    "mean_rankic", "rankic_std", "rankic_ir", "positive_rankic_ratio",
    "total_return", "annualized_return", "annualized_excess_return",
    "annualized_volatility", "maximum_drawdown", "tracking_error",
    "information_ratio", "sharpe_ratio", "turnover", "transaction_cost", "failed_orders",
)


logger = logging.getLogger(__name__)


class Metrics:
    def __init__(self, trading_days_per_year: int, risk_free_rate: float):
        self.trading_days_per_year = trading_days_per_year
        self.risk_free_rate = risk_free_rate

    def calculate(self, tables: dict[str, pd.DataFrame]) -> dict[str, float | int | None]:
        # TODO: 从 RankIC 序列计算均值、标准差、IR、正值比例；不年化 RankICIR。
        # 从净值/收益计算累计、年化、超额收益、波动率、最大回撤、Sharpe。
        # active = portfolio_return - benchmark_return。
        # TE = std(active) * sqrt(N), IR = mean(active) / std(active) * sqrt(N)。
        # 从实际成交/订单汇总换手、费用及失败订单；零方差等未定义指标保留 None。
        # 仅输出原始指标；赛制层决定排行榜权重，不在此实现私榜加权。
        logger.debug("[Metrics.calculate] STUB all 15 metrics are None (not calculated)")
        return dict.fromkeys(METRIC_NAMES)
