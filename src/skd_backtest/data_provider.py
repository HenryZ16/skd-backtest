"""Research data and engine-only market data are separate interfaces."""

from pathlib import Path

import pandas as pd

from .config import BacktestConfig
from .schemas import SOURCE_COLUMNS


class DataProvider:
    def __init__(self, config: BacktestConfig):
        self.config = config

    def monthly_path(self, dataset: str, year: int, month: int) -> Path:
        return self.config.data_dir / dataset / str(year) / f"{month:02d}" / f"{year}{month:02d}.parquet"

    def prepare(self) -> list[str]:
        """Read each relevant monthly date column once; return the actual calendar."""
        start = int(self.config.start_date.replace("-", ""))
        end = int(self.config.end_date.replace("-", ""))
        dates = pd.concat([
            pd.read_parquet(
                self.monthly_path("MarketData", month.year, month.month),
                columns=["日期"], filters=[("日期", ">=", start), ("日期", "<=", end)],
            )["日期"]
            for month in pd.period_range(self.config.start_date, self.config.end_date, freq="M")
        ], ignore_index=True).drop_duplicates().sort_values()
        trading_dates = pd.to_datetime(dates, format="%Y%m%d").dt.strftime("%Y-%m-%d").tolist()
        print(f"[DataProvider.prepare] {self.config.start_date} .. {self.config.end_date}; "
              f"{len(trading_dates)} trading days from {self.config.data_dir}")
        # TODO: 按 lookback 读取预热历史，缓存研究数据；按 holding_period 准备标签。
        return trading_dates

    def as_of(self, as_of_date: str) -> dict[str, pd.DataFrame]:
        # TODO: 只返回截至 as_of_date 的研究历史，财务数据遵守披露日。
        # 当日 Barra 成分定义合法股票池；不能把年度并集当作当日成分。
        # 不向 inference 传入数据路径、交易数据、未来标签或整个 DataProvider。
        print(f"[DataProvider.as_of] STUB {as_of_date}; empty research tables")
        return {name: pd.DataFrame(columns=columns) for name, columns in SOURCE_COLUMNS.items()}

    def portfolio_inputs(self, as_of_date: str) -> dict[str, pd.DataFrame]:
        # TODO: 返回当日真实基准权重、行业和 Barra 暴露；调出成分目标权重归零。
        # 当前数据没有基准权重，不能用等权冒充真实指数权重。
        print(f"[DataProvider.portfolio_inputs] STUB {as_of_date}")
        return {
            "benchmark_weights": pd.DataFrame(columns=["code", "benchmark_weight"]),
            "barra_exposures": pd.DataFrame(columns=SOURCE_COLUMNS["Barra_factor"]),
            "industries": pd.DataFrame(columns=["code", "industry"]),
        }

    def open_market(self, date: str) -> pd.DataFrame:
        # TODO: 真实模式提供不复权开盘价、PIT 涨跌停价、停牌/ST/板块状态。
        # 只能使用开盘时已知信息，不能携带当日 high/low/close 决定成交。
        # adjusted_return 模式使用复权收益路径，不能据此生成真实股数交易。
        print(f"[DataProvider.open_market] STUB date={date}")
        return pd.DataFrame(columns=[
            "date", "code", "raw_open", "upper_limit", "lower_limit",
            "is_suspended", "is_st", "board",
        ])

    def close_market(self, date: str) -> pd.DataFrame:
        # TODO: 返回当日估值价格及基准收益；停牌持仓使用最近有效价格。
        print(f"[DataProvider.close_market] STUB date={date}")
        return pd.DataFrame(columns=["date", "code", "close", "benchmark_return"])

    def corporate_actions(self, date: str) -> pd.DataFrame:
        # TODO: 提供真实价格模式下当日生效的分红、送转、拆并股及配股事件。
        print(f"[DataProvider.corporate_actions] STUB date={date}")
        return pd.DataFrame(columns=["date", "code", "action", "cash_per_share", "share_ratio"])

    def future_returns(self, holding_period: int) -> pd.DataFrame:
        # TODO: 仅供事后评估：Open(t+H+1) / Open(t+1) - 1，H 按交易日计数。
        # 缺少完整标签窗口的信号不参与 RankIC；标签绝不进入 inference。
        print(f"[DataProvider.future_returns] STUB H={holding_period}; evaluator only")
        return pd.DataFrame(columns=["date", "code", "future_return"])
