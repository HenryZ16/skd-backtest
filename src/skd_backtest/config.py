"""Immutable configuration shared by the engine and component contracts."""

from dataclasses import dataclass
from datetime import date
from math import isfinite
from pathlib import Path
from typing import Literal


REFERENCE_DATASETS = {
    "benchmark_returns": "HS300_index",
    "benchmark_weights": "HS300_weight",
    "industries": "HS300_industry",
}


@dataclass(frozen=True)
class OptimizerConfig:
    method: Literal["top_k", "barra"] = "top_k"
    top_k: int = 50
    long_only: bool = True
    fully_invested: bool = True
    single_name_weight_limit: float | None = None
    active_weight_limit: float | None = None
    industry_exposure_limit: float | None = None
    barra_style_exposure_limit: float | None = None
    turnover_limit: float | None = None


@dataclass(frozen=True)
class FeeScheduleEntry:
    """Rates effective from an ISO date; entries supplied in date order."""

    effective_date: str
    stamp_tax_rate: float
    transfer_fee_rate: float


@dataclass(frozen=True)
class CostConfig:
    commission_rate: float = 0.0
    minimum_commission: float = 0.0
    slippage: float = 0.0
    fee_schedule: tuple[FeeScheduleEntry, ...] = ()


@dataclass(frozen=True)
class BacktestConfig:
    data_dir: Path
    start_date: str
    end_date: str
    initial_cash: float
    rebalance_interval: int
    holding_period: int
    lookback: int
    price_mode: Literal["adjusted_return", "raw_price"]
    trading_days_per_year: int
    risk_free_rate: float
    output_dir: Path | None
    read_batch_months: int = 12
    prefetch: bool = True
    async_inference: bool = True
    random_seed: int = 0
    label_price_basis: Literal["adjusted_open", "raw_open"] = "adjusted_open"
    friendly_output: bool = True

    def __post_init__(self):
        object.__setattr__(self, "data_dir", Path(self.data_dir))
        if self.output_dir is not None:
            object.__setattr__(self, "output_dir", Path(self.output_dir))
        for value in (self.start_date, self.end_date):
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError("dates must use YYYY-MM-DD")
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        for name in ("lookback", "rebalance_interval", "holding_period",
                     "read_batch_months", "trading_days_per_year"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("initial_cash must be finite and positive")
        if not isfinite(self.risk_free_rate) or self.risk_free_rate <= -1:
            raise ValueError("risk_free_rate must be finite and greater than -1")
        if type(self.random_seed) is not int or not 0 <= self.random_seed < 2**32:
            raise ValueError("random_seed must be an integer in [0, 2**32)")
        if not isinstance(self.async_inference, bool):
            raise TypeError("async_inference must be a boolean")
        if not isinstance(self.friendly_output, bool):
            raise TypeError("friendly_output must be a boolean")
        for name, allowed in (
            ("price_mode", ("adjusted_return", "raw_price")),
            ("label_price_basis", ("adjusted_open", "raw_open")),
        ):
            if getattr(self, name) not in allowed:
                raise ValueError(f"invalid {name}")

    def validate_data(self, optimizer: OptimizerConfig) -> None:
        references = ["benchmark_returns"]
        if optimizer.method == "barra":
            references.append("benchmark_weights")
        if optimizer.industry_exposure_limit is not None:
            references.append("industries")
        for name in references:
            path = self.data_dir / REFERENCE_DATASETS[name]
            if not path.is_dir():
                raise ValueError(f"{name} requires data directory: {path}")
