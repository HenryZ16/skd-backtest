"""Point-in-time benchmark and portfolio reference data."""

from datetime import date as Date
from math import fsum, isclose, isfinite
from pathlib import Path

import pandas as pd
from pandas.api.types import is_bool_dtype, is_numeric_dtype

from .config import BacktestConfig
from .contracts import BenchmarkDay, Dataset, PortfolioInputs, Topic
from .runtime_cache import CacheView


_REQUIRED_COLUMNS = {
    "benchmark_returns": ("date", "benchmark_return"),
    "benchmark_weights": ("date", "code", "benchmark_weight"),
    "industries": ("date", "code", "industry"),
    "factor_covariance": ("date", "factor1", "factor2", "covariance"),
    "specific_risk": ("date", "code", "specific_variance"),
}
_KEY_COLUMNS = {
    "benchmark_returns": ("date",),
    "benchmark_weights": ("date", "code"),
    "industries": ("date", "code"),
    "factor_covariance": ("date", "factor1", "factor2"),
    "specific_risk": ("date", "code"),
}


class ReferenceDataProvider:
    """Load each configured source once per run and expose exact-date slices."""

    def __init__(self, config: BacktestConfig):
        self.config = config
        self._tables = {}
        self._slices = {}

    def _load(self, name: str) -> dict[str, pd.DataFrame] | None:
        if name in self._slices:
            return self._slices[name]

        path = getattr(self.config.reference_sources, name)
        if path is None:
            if getattr(self.config.data_capabilities, name):
                raise ValueError(f"{name} capability is declared but no source is configured")
            return None

        path = Path(path)
        if path.suffix.lower() == ".csv":
            frame = pd.read_csv(path, dtype={"date": "string", "code": "string"})
        elif path.suffix.lower() == ".parquet":
            frame = pd.read_parquet(path)
        else:
            raise ValueError(f"{name} source must be a CSV or Parquet file")

        self._validate(name, frame)
        frame = frame.sort_values(list(_KEY_COLUMNS[name]), kind="stable", ignore_index=True)
        slices = {}
        dates = frame["date"].tolist()
        start = 0
        for index in range(1, len(dates) + 1):
            if index == len(dates) or dates[index] != dates[start]:
                slices[dates[start]] = frame.iloc[start:index]
                start = index

        self._tables[name] = frame
        self._slices[name] = slices
        return slices

    @staticmethod
    def _validate(name: str, frame: pd.DataFrame) -> None:
        required = _REQUIRED_COLUMNS[name]
        if not frame.columns.is_unique or any(column not in frame.columns for column in required):
            raise ValueError(f"{name} source is missing required columns or has duplicate columns")

        dates = frame["date"]
        if dates.isna().any() or not all(
            isinstance(value, str) and _is_iso_date(value) for value in dates.drop_duplicates()
        ):
            raise ValueError(f"{name} dates must be YYYY-MM-DD strings")
        if frame.duplicated(list(_KEY_COLUMNS[name])).any():
            raise ValueError(f"{name} source contains duplicate keys")

        if "code" in required:
            codes = frame["code"]
            if codes.isna().any() or not all(
                isinstance(value, str) and value for value in codes
            ):
                raise ValueError(f"{name} codes must be nonempty strings")

        if name == "industries":
            values = frame["industry"]
            if values.isna().any() or not all(
                isinstance(value, str) and value for value in values
            ):
                raise ValueError("industries must contain nonempty strings")
            return

        if name == "factor_covariance":
            for column in ("factor1", "factor2"):
                if not all(isinstance(value, str) and value for value in frame[column]):
                    raise ValueError("risk factor names must be nonempty strings")
        value_column = {
            "benchmark_returns": "benchmark_return", "benchmark_weights": "benchmark_weight",
            "factor_covariance": "covariance", "specific_risk": "specific_variance",
        }[name]
        values = frame[value_column]
        if not is_numeric_dtype(values.dtype) or is_bool_dtype(values.dtype):
            raise ValueError(f"{value_column} values must be numeric")
        try:
            numeric = [float(value) for value in values]
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{value_column} values must be finite numbers") from None
        if any(not isfinite(value) for value in numeric):
            raise ValueError(f"{value_column} values must be finite numbers")
        if name == "benchmark_returns" and any(value < -1 for value in numeric):
            raise ValueError("benchmark returns must be at least -1")
        if name == "specific_risk" and any(value < 0 for value in numeric):
            raise ValueError("specific variances must be nonnegative")
        if name == "benchmark_weights":
            if any(value < 0 for value in numeric):
                raise ValueError("benchmark weights must be nonnegative")
            for _, daily in frame.groupby("date", sort=False):
                if not isclose(fsum(float(value) for value in daily.benchmark_weight),
                               1.0, rel_tol=0.0, abs_tol=1e-6):
                    raise ValueError("benchmark weights must sum to one for each date")

    def _external(self, name: str, date: str) -> Dataset:
        slices = self._load(name)
        if slices is None:
            return Dataset("unavailable", None, f"{name} source is not configured")
        data = slices.get(date)
        if data is None:
            raise ValueError(f"{name} source has no data for {date}")
        return Dataset("available", data)

    def prepare_close(self, *, date: str, cache: CacheView) -> None:
        if self.config.benchmark_mode == "none":
            data = Dataset("unavailable", None, "benchmark_mode=none")
        else:
            source = self._external("benchmark_returns", date)
            if source.status == "unavailable":
                data = source
            else:
                value = float(source.data["benchmark_return"].iloc[0])
                data = Dataset("available", BenchmarkDay(date, value))
        cache.publish(Topic.REFERENCE_BENCHMARK, date, data)

    def prepare_signal(self, *, date: str, cache: CacheView) -> None:
        market = cache.read(Topic.MARKET_CONTEXT, date)
        universe = market.universe
        legal_codes = set(universe["code"])

        weights = (
            self._external("benchmark_weights", date)
            if self.config.benchmark_mode == "csi300"
            else Dataset("unavailable", None, "benchmark_mode=none")
        )
        if weights.status == "available" and set(weights.data.code) != legal_codes:
            raise ValueError("benchmark weights must cover the legal universe exactly once")

        industries = self._external("industries", date)
        if industries.status == "available":
            industry_codes = set(industries.data.code)
            if not legal_codes.issubset(industry_codes):
                raise ValueError("industries are missing codes from the legal universe")
            if industry_codes != legal_codes:
                industries = Dataset(
                    "available", industries.data.loc[industries.data.code.isin(legal_codes)],
                )

        cache.publish(Topic.REFERENCE_PORTFOLIO, date, PortfolioInputs(
            date,
            universe,
            Dataset("available", market.barra_exposures),
            weights,
            industries,
            self._external("factor_covariance", date),
            self._external("specific_risk", date),
        ))

    def close(self) -> None:
        self._tables.clear()
        self._slices.clear()


def _is_iso_date(value: str) -> bool:
    try:
        return Date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False