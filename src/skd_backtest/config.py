"""Backtest configuration and playback bounds."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class OptimizerConfig:
    method: str = "top_k"
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

    def __post_init__(self):
        if date.fromisoformat(self.start_date) > date.fromisoformat(self.end_date):
            raise ValueError("start_date must not be after end_date")
        for name in ("lookback", "rebalance_interval", "holding_period", "read_batch_months"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
