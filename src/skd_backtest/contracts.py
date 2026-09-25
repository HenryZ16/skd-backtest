"""Public data packets and names. No component or financial algorithm imports."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeAlias, TypeVar

import pandas as pd

from .config import BacktestConfig, CostConfig, OptimizerConfig

PROTOCOL_VERSION = "1"
CacheKey: TypeAlias = str | tuple[str, int] | None
T = TypeVar("T")


class Phase(StrEnum):
    INITIALIZE = "INITIALIZE"
    PRE_OPEN = "PRE_OPEN"
    OPEN_VALUE = "OPEN_VALUE"
    EXECUTION = "EXECUTION"
    CLOSE_VALUE = "CLOSE_VALUE"
    SIGNAL = "SIGNAL"
    EVALUATION = "EVALUATION"
    METRICS = "METRICS"
    OUTPUT = "OUTPUT"
    FAILED = "FAILED"
    CLOSED = "CLOSED"


class ComponentRole(StrEnum):
    ENGINE = "engine"
    REFERENCE_DATA = "reference_data"
    CORPORATE_ACTIONS = "corporate_actions"
    BROKER = "broker"
    ACCOUNTING = "accounting"
    RUNNER = "runner"
    OPTIMIZER = "optimizer"
    COST_MODEL = "cost_model"
    LABEL_PROVIDER = "label_provider"
    EVALUATOR = "evaluator"
    METRICS = "metrics"
    WRITER = "writer"


class Topic(StrEnum):
    RUN_CONTEXT = "run.context"
    RUN_CALENDAR = "run.calendar"
    ACCOUNT_INITIAL = "account.initial"
    MARKET_CONTEXT = "market.context"
    REFERENCE_ACTIONS = "reference.actions"
    REFERENCE_BENCHMARK = "reference.benchmark"
    REFERENCE_PORTFOLIO = "reference.portfolio"
    ACCOUNT_ACTIONS = "account.actions"
    ACCOUNT_SETTLED = "account.settled"
    ACCOUNT_OPEN = "account.open"
    EXECUTION_DAY = "execution.day"
    ACCOUNT_CLOSE = "account.close"
    SIGNAL_SCORES = "signal.scores"
    SIGNAL_TARGETS = "signal.targets"
    COST_REQUEST = "cost.request"
    COST_RESULT = "cost.result"
    EVALUATION_LABELS = "evaluation.labels"
    EVALUATION_PREDICTION = "evaluation.prediction"
    EVALUATION_METRICS = "evaluation.metrics"
    OUTPUT_RECEIPT = "output.receipt"


@dataclass(frozen=True)
class Dataset(Generic[T]):
    status: str
    data: T | None
    reason: str | None = None

    def __post_init__(self):
        if self.status == "available":
            if self.data is None or self.reason is not None:
                raise ValueError("available dataset requires data and no reason")
        elif self.status == "unavailable":
            if self.data is not None or not isinstance(self.reason, str) or not self.reason:
                raise ValueError("unavailable dataset requires a reason and no data")
        else:
            raise ValueError("invalid dataset status")


@dataclass(frozen=True)
class RunContext:
    backtest: BacktestConfig
    optimizer: OptimizerConfig
    costs: CostConfig
    protocol_version: str = PROTOCOL_VERSION

    @property
    def initial_cash(self):
        return self.backtest.initial_cash

    @property
    def price_mode(self):
        return self.backtest.price_mode

    @property
    def benchmark_mode(self):
        return self.backtest.benchmark_mode

    @property
    def label_price_basis(self):
        return self.backtest.label_price_basis

    @property
    def rights_policy(self):
        return self.backtest.rights_policy

    @property
    def data_capabilities(self):
        return self.backtest.data_capabilities

    @property
    def reference_sources(self):
        return self.backtest.reference_sources


@dataclass(frozen=True)
class RunCalendar:
    trading_dates: tuple[str, ...]
    signal_calendar: pd.DataFrame

    @classmethod
    def from_dates(cls, dates, rebalance_interval):
        dates = tuple(dates)
        rows = [(dates[i], dates[i + 1]) for i in range(0, max(0, len(dates) - 1), rebalance_interval)]
        return cls(dates, pd.DataFrame(rows, columns=["signal_date", "execution_date"]))


@dataclass(frozen=True)
class AccountState:
    price_mode: str
    cash: float
    positions: pd.DataFrame
    locked_lots: pd.DataFrame


@dataclass(frozen=True)
class InitialAccount:
    account: AccountState
    portfolio_value: float
    portfolio_nav: float
    benchmark_nav: float | None


@dataclass(frozen=True)
class ActionResult:
    date: str
    account: AccountState
    events: pd.DataFrame


@dataclass(frozen=True)
class OpenSnapshot:
    date: str
    account: AccountState
    values: pd.DataFrame
    market_value: float
    portfolio_value: float


@dataclass(frozen=True)
class ExecutionResult:
    date: str
    account: AccountState
    orders: pd.DataFrame
    trades: pd.DataFrame
    trade_value: float
    total_cost: float
    executed_signal_date: str | None


@dataclass(frozen=True)
class CloseSnapshot:
    date: str
    account: AccountState
    positions: pd.DataFrame
    equity_curve: pd.DataFrame
    actual_weights: pd.DataFrame


@dataclass(frozen=True)
class MarketContext:
    date: str
    universe: pd.DataFrame
    barra_exposures: pd.DataFrame


@dataclass(frozen=True)
class PortfolioInputs:
    date: str
    universe: pd.DataFrame
    barra_exposures: Dataset[pd.DataFrame]
    benchmark_weights: Dataset[pd.DataFrame]
    industries: Dataset[pd.DataFrame]


@dataclass(frozen=True)
class BenchmarkDay:
    date: str
    benchmark_return: float


Scores: TypeAlias = pd.DataFrame
Labels: TypeAlias = pd.DataFrame


@dataclass(frozen=True)
class TargetPlan:
    signal_date: str
    execution_date: str
    weights: pd.DataFrame


@dataclass(frozen=True)
class CostRequest:
    request_id: int
    order_id: str
    date: str
    side: str
    price_mode: str
    base_price: float | None
    shares: int | None
    position_value: float | None


@dataclass(frozen=True)
class CostQuote:
    request_id: int
    order_id: str
    date: str
    side: str
    price_mode: str
    execution_price: float | None
    position_value: float
    trade_value: float
    commission: float
    stamp_tax: float
    other_cost: float
    total_cost: float
    cash_delta: float


@dataclass(frozen=True)
class PredictionResult:
    predictions: pd.DataFrame
    rankic: pd.DataFrame


@dataclass(frozen=True)
class OutputReceipt:
    status: str
    output_dir: str | None
    files: dict[str, str]


@dataclass(frozen=True)
class LogRecord:
    seq: int
    date: str | None
    phase: Phase
    component: ComponentRole
    level: str
    message: str
    details: dict
