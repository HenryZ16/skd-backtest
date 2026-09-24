"""Top-down composition root. This release runs one skeleton walkthrough."""

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

    def run(self) -> dict[str, float | int | None]:
        """Walk through every module once; return all 15 uncalculated metrics."""
        print("[BacktestEngine.run] SKELETON ONLY: one walkthrough, no real backtest")
        self.metrics = None
        self.tables = {}
        self.account = self.broker.initialize(self.config.initial_cash)
        self.data_provider.prepare()

        # TODO: 用真实交易日日历替代本次单次演示。每日先公司行为/开盘执行昨日
        # 目标，再收盘估值；调仓日收盘提供 as-of 数据并产生下一交易日待执行目标。
        # 非调仓日仍估值；末日没有区间内下一交易日时不再产生可执行调仓。
        # 当前 start_date 只是演示标签，不声称它是交易日；end_date 仅存入配置。
        signal_date = self.config.start_date
        execution_date = None  # 由未来的真实日历确定，不能伪造为自然日 + 1。

        research_data = self.data_provider.as_of(signal_date)
        scores = self.submission_runner.predict(signal_date, research_data)
        portfolio_inputs = self.data_provider.portfolio_inputs(signal_date)
        targets = self.optimizer.optimize(
            scores=scores, **portfolio_inputs,
            current_weights=self.accounting.current_weights(self.account),
        )
        self.tables["target_weights"] = targets

        print("[BacktestEngine.next_open] STUB transition to the next trading day's open")
        self.corporate_actions.apply(
            date=execution_date, actions=self.data_provider.corporate_actions(execution_date),
            account=self.account, price_mode=self.config.price_mode,
        )
        self.tables["orders"], self.tables["trades"] = self.broker.execute(
            date=execution_date, target_weights=targets,
            market=self.data_provider.open_market(execution_date), account=self.account,
            cost_model=self.cost_model, price_mode=self.config.price_mode,
        )
        self.tables["positions"], self.tables["equity_curve"] = self.accounting.mark_to_market(
            date=execution_date, account=self.account,
            market=self.data_provider.close_market(execution_date), price_mode=self.config.price_mode,
        )

        # 标签只在事后评估阶段读取，与模型/优化器的输入隔离。
        self.tables["predictions"], self.tables["rankic"] = self.prediction_evaluator.evaluate(
            scores=scores, future_returns=self.data_provider.future_returns(self.config.holding_period),
        )
        self.metrics = self.metrics_calculator.calculate(self.tables)
        self.result_writer.write(metrics=self.metrics, tables=self.tables)
        print("[BacktestEngine.run] skeleton walkthrough complete")
        return self.metrics
