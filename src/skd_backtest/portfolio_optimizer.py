"""Choose target weights independently of order execution."""

import pandas as pd

from .config import OptimizerConfig
from .schemas import empty_result


class PortfolioOptimizer:
    def __init__(self, config: OptimizerConfig):
        self.config = config

    def optimize(self, *, scores: pd.DataFrame, benchmark_weights: pd.DataFrame,
                 barra_exposures: pd.DataFrame, industries: pd.DataFrame,
                 current_weights: pd.DataFrame) -> pd.DataFrame:
        # TODO: score 截面排名 -> 百分位/正态分数 -> Top-K 或 benchmark tilt。
        # 后续实现配置中的个股、主动权重、行业、风格及换手限制，不修改 Broker。
        # Long Only / Fully Invested 同样由配置控制；不把 score 当作预期收益。
        print(f"[PortfolioOptimizer.optimize] STUB method={self.config.method}")
        return empty_result("target_weights")
