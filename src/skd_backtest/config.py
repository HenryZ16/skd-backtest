"""Immutable configuration shared by the engine and component contracts."""

from dataclasses import dataclass, field
from datetime import date
from math import isfinite
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
    risk_aversion: float = 1.0
    barra_factors: tuple[str, ...] = (
        "市值", "贝塔", "动量", "残差波动", "非线性市值",
        "账面市值比", "流动性", "盈利", "成长", "杠杆",
    )


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
class DataCapabilities:
    """Declared source coverage; source adapters must verify actual availability."""

    adjusted_prices: bool = True
    raw_prices: bool = False
    adjustment_factors: bool = False
    price_limits: bool = False
    suspension: bool = True
    constituents: bool = True
    barra_exposures: bool = True
    benchmark_returns: bool = False
    benchmark_weights: bool = False
    industries: bool = False
    factor_covariance: bool = False
    specific_risk: bool = False


@dataclass(frozen=True)
class ReferenceSources:
    benchmark_returns: Path | None = None
    benchmark_weights: Path | None = None
    industries: Path | None = None
    factor_covariance: Path | None = None
    specific_risk: Path | None = None

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value))


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
    benchmark_mode: Literal["none", "csi300"] = "none"
    label_price_basis: Literal["adjusted_open", "raw_open"] = "adjusted_open"
    data_capabilities: DataCapabilities = field(default_factory=DataCapabilities)
    reference_sources: ReferenceSources = field(default_factory=ReferenceSources)

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
        for name, allowed in (
            ("price_mode", ("adjusted_return", "raw_price")),
            ("benchmark_mode", ("none", "csi300")),
            ("label_price_basis", ("adjusted_open", "raw_open")),
        ):
            if getattr(self, name) not in allowed:
                raise ValueError(f"invalid {name}")
        if not isinstance(self.data_capabilities, DataCapabilities):
            raise TypeError("data_capabilities must be DataCapabilities")
        if not isinstance(self.reference_sources, ReferenceSources):
            raise TypeError("reference_sources must be ReferenceSources")

    def validate_capabilities(self, optimizer: OptimizerConfig) -> None:
        capabilities = self.data_capabilities
        required = ["adjusted_prices", "suspension", "constituents"]
        if self.price_mode == "raw_price" or self.label_price_basis == "raw_open":
            if not (capabilities.raw_prices or capabilities.adjustment_factors):
                raise NotImplementedError("raw_price requires raw OHLC/adjustment factors")
        if self.price_mode == "raw_price":
            required += ["price_limits"]
        if self.benchmark_mode == "csi300":
            required += ["benchmark_returns"]
        needs_weights = (optimizer.method != "top_k" or optimizer.active_weight_limit is not None
                         or optimizer.industry_exposure_limit is not None
                         or optimizer.barra_style_exposure_limit is not None)
        if needs_weights:
            if self.benchmark_mode == "none":
                raise ValueError("benchmark-relative optimization requires benchmark_mode=csi300")
            required += ["benchmark_weights"]
        if optimizer.method == "barra":
            required += ["barra_exposures", "factor_covariance", "specific_risk"]
        if optimizer.industry_exposure_limit is not None:
            required += ["industries"]
        if optimizer.barra_style_exposure_limit is not None:
            required += ["barra_exposures"]
        missing = [name for name in required if not getattr(capabilities, name)]
        if missing:
            raise ValueError("missing data capabilities: " + ", ".join(missing))
        for name in ReferenceSources.__dataclass_fields__:
            if getattr(capabilities, name) and getattr(self.reference_sources, name) is None:
                raise ValueError(f"declared {name} capability requires a reference source")
