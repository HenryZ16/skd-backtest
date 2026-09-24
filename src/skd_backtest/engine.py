"""Daily backtest control flow with placeholder financial modules."""

from pathlib import Path
from typing import Literal

import pandas as pd

from .accounting import PortfolioAccounting
from .broker import Broker
from .config import BacktestConfig, CostConfig, OptimizerConfig
from .corporate_actions import CorporateActionEngine
from .cost_model import CostModel
from .data_provider import DataProvider
from .metrics import Metrics
from .portfolio_optimizer import PortfolioOptimizer
from .prediction_evaluator import PredictionEvaluator
from .result_writer import ResultWriter
from .schemas import RESULT_COLUMNS, empty_result
from .submission_runner import Inference, SubmissionRunner


class BacktestEngine:
    """Configure once, call run(), read the returned metrics or engine.metrics.

    inference is a callable accepting keyword arguments as_of_date and data.
    Pass model.predict when using the specification's InferenceModel object.
    Dates use YYYY-MM-DD; interval, horizon and lookback use trading days.
    Configuration is trusted. Errors propagate directly to the caller.
    """

    def __init__(
        self, *, data_dir: str | Path, start_date: str, end_date: str,
        inference: Inference, initial_cash: float = 1_000_000.0,
        rebalance_interval: int = 5, holding_period: int = 5, lookback: int = 252,
        price_mode: Literal["adjusted_return", "raw_price"] = "adjusted_return",
        optimizer_config: OptimizerConfig | None = None,
        cost_config: CostConfig | None = None,
        trading_days_per_year: int = 252, risk_free_rate: float = 0.0,
        output_dir: str | Path | None = None,
    ):
        self.config = BacktestConfig(
            data_dir=Path(data_dir), start_date=start_date, end_date=end_date,
            initial_cash=initial_cash, rebalance_interval=rebalance_interval,
            holding_period=holding_period, lookback=lookback, price_mode=price_mode,
            trading_days_per_year=trading_days_per_year, risk_free_rate=risk_free_rate,
            output_dir=Path(output_dir) if output_dir is not None else None,
        )
        self.submission_runner = SubmissionRunner(inference)
        self.data_provider = DataProvider(self.config)
        self.prediction_evaluator = PredictionEvaluator()
        self.optimizer = PortfolioOptimizer(optimizer_config or OptimizerConfig())
        self.broker = Broker()
        self.corporate_actions = CorporateActionEngine()
        self.cost_model = CostModel(cost_config or CostConfig())
        self.accounting = PortfolioAccounting()
        self.metrics_calculator = Metrics(trading_days_per_year, risk_free_rate)
        self.result_writer = ResultWriter(self.config.output_dir)
        self.metrics: dict[str, float | int | None] | None = None
        self.tables: dict[str, pd.DataFrame] = {}
        self.account: dict = {}
        self.trading_dates: list[str] = []

    def run(self) -> dict[str, float | int | None]:
        """Visit every trading day; financial calculations remain placeholders."""
        print("[BacktestEngine.run] daily loop; financial modules are STUBs")
        self.metrics = None
        self.tables = {}
        self.trading_dates = []
        self.account = self.broker.initialize(self.config.initial_cash)
        self.trading_dates = self.data_provider.prepare()
        records = {name: [] for name in RESULT_COLUMNS}
        pending_targets = None

        for day_index, date in enumerate(self.trading_dates):
            print(f"[BacktestEngine.day] {date}")
            self.corporate_actions.apply(
                date=date, actions=self.data_provider.corporate_actions(date),
                account=self.account, price_mode=self.config.price_mode,
            )
            market = self.data_provider.open_market(date)
            self.broker.start_day(date=date, account=self.account)
            # None 表示无待执行调仓；空目标表仍然是一次已排期的优化结果。
            if pending_targets is not None:
                orders, trades = self.broker.execute(
                    date=date, target_weights=pending_targets, market=market,
                    account=self.account, cost_model=self.cost_model, price_mode=self.config.price_mode,
                )
                records["orders"].append(orders)
                records["trades"].append(trades)
                pending_targets = None

            positions, equity = self.accounting.mark_to_market(
                date=date, account=self.account,
                market=self.data_provider.close_market(date), price_mode=self.config.price_mode,
            )
            records["positions"].append(positions)
            records["equity_curve"].append(equity)

            # 从区间首个真实交易日起按交易日计数；末日仅估值，不产生区间外调仓。
            if day_index % self.config.rebalance_interval == 0 and day_index + 1 < len(self.trading_dates):
                scores = self.submission_runner.predict(date, self.data_provider.as_of(date))
                records["predictions"].append(scores)
                pending_targets = self.optimizer.optimize(
                    scores=scores, **self.data_provider.portfolio_inputs(date),
                    current_weights=self.accounting.current_weights(self.account),
                ).assign(signal_date=date, execution_date=self.trading_dates[day_index + 1])
                records["target_weights"].append(pending_targets)

        # 循环结束后统一拼接，避免逐日复制不断增长的审计表。
        self.tables = {
            name: pd.concat(parts, ignore_index=True) if parts else empty_result(name)
            for name, parts in records.items()
        }

        # 标签只在事后评估阶段读取，与模型/优化器的输入隔离。
        self.tables["predictions"], self.tables["rankic"] = self.prediction_evaluator.evaluate(
            scores=self.tables["predictions"],
            future_returns=self.data_provider.future_returns(self.config.holding_period),
        )
        self.metrics = self.metrics_calculator.calculate(self.tables)
        self.result_writer.write(metrics=self.metrics, tables=self.tables)
        print(f"[BacktestEngine.run] completed {len(self.trading_dates)} trading days; metrics remain STUBs")
        return self.metrics
