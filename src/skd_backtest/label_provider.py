"""Build evaluator-only forward-return labels from independent market reads."""

from datetime import date
import math
from pathlib import Path

import pandas as pd

from .config import BacktestConfig
from .contracts import Topic
from .runtime_cache import CacheView
from .schemas import LABEL_COLUMNS


def _month_key(path: Path) -> tuple[int, int]:
    year, month = int(path.parent.parent.name), int(path.parent.name)
    if not 1 <= month <= 12 or path.stem != f"{year}{month:02d}":
        raise ValueError(f"invalid MarketData monthly path: {path}")
    return year, month


def _next_month(month: tuple[int, int]) -> tuple[int, int]:
    year, number = month
    return (year + 1, 1) if number == 12 else (year, number + 1)


def _source_date(value, month: tuple[int, int]) -> str:
    try:
        number = int(value)
        text = f"{number:08d}"
        parsed = date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid MarketData 日期 value: {value!r}") from exc
    if parsed.strftime("%Y%m") != f"{month[0]}{month[1]:02d}":
        raise ValueError(f"MarketData date {number} is stored in the wrong month")
    return parsed.isoformat()


def _signal_month(value: str) -> tuple[int, int]:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"signal date must use YYYY-MM-DD: {value!r}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"signal date must use YYYY-MM-DD: {value!r}")
    return parsed.year, parsed.month


def _positive_finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _suspension_state(value) -> bool | None:
    if pd.isna(value):
        return None
    if isinstance(value, (bool, int, float)) and value in (0, 1):
        return bool(value)
    return None


class LabelProvider:
    def __init__(self, config: BacktestConfig):
        self.config = config

    def _monthly_files(self) -> dict[tuple[int, int], Path]:
        root = self.config.data_dir / "MarketData"
        files = {}
        for path in root.glob("*/*/*.parquet"):
            month = _month_key(path)
            if month in files:
                raise ValueError(f"duplicate MarketData monthly file: {month}")
            files[month] = path
        if not files:
            raise FileNotFoundError(f"no MarketData monthly files under {root}")
        return files

    @staticmethod
    def _read_calendar_month(path: Path, month: tuple[int, int]) -> list[str]:
        table = pd.read_parquet(path, columns=["日期"])
        if "日期" not in table.columns:
            raise ValueError(f"MarketData file {path} has no 日期 column")
        if table["日期"].isna().any():
            raise ValueError(f"MarketData file {path} contains a missing 日期")
        dates = sorted({
            _source_date(value, month)
            for value in table["日期"].drop_duplicates().tolist()
        })
        return dates

    @staticmethod
    def _price_source(basis: str, capabilities) -> tuple[str, bool]:
        if basis == "adjusted_open":
            return "open", False
        if capabilities.raw_prices:
            return "raw_open", False
        if capabilities.adjustment_factors:
            return "open", True
        raise ValueError("raw_open labels require raw_prices or adjustment_factors")

    def _read_endpoint_prices(
        self, endpoints: pd.DataFrame, files: dict[tuple[int, int], Path],
        basis: str, capabilities,
    ) -> pd.DataFrame:
        source_column, use_factor = self._price_source(basis, capabilities)
        columns = ["日期", "代码", source_column, "is_suspend"]
        if use_factor:
            columns.append("adjustment_factor")

        parts = []
        grouped = endpoints.assign(month=endpoints["date"].str[:7]).groupby("month", sort=True)
        for month_text, requests in grouped:
            year, month = map(int, month_text.split("-"))
            month_key = (year, month)
            path = files[month_key]
            date_map = {
                int(value.replace("-", "")): value
                for value in requests["date"].drop_duplicates().tolist()
            }
            requested_codes = requests["code"].drop_duplicates().tolist()
            table = pd.read_parquet(
                path,
                columns=columns,
                filters=[
                    ("日期", "in", list(date_map)),
                    ("代码", "in", requested_codes),
                ],
            )
            missing = set(columns) - set(table.columns)
            if missing:
                raise ValueError(f"MarketData file {path} is missing columns: {sorted(missing)}")

            table["date"] = table["日期"].map(date_map)
            if table["date"].isna().any():
                raise ValueError(f"MarketData file {path} returned rows outside requested label dates")
            table["code"] = table["代码"]
            table["present"] = True
            table["source_price"] = table[source_column]
            if use_factor:
                table["factor"] = table["adjustment_factor"]
            keep = ["date", "code", "present", "is_suspend", "source_price"]
            if use_factor:
                keep.append("factor")
            parts.append(table.loc[:, keep])

        price_columns = ["date", "code", "present", "is_suspend", "source_price"]
        if use_factor:
            price_columns.append("factor")
        prices = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=price_columns)
        if prices.duplicated(["date", "code"]).any():
            raise ValueError("MarketData has duplicate date/code rows for label endpoints")
        return prices

    @staticmethod
    def _endpoint_value(
        present, suspension, source_price, factor, *, kind: str, use_factor: bool,
    ) -> tuple[float | None, str | None]:
        prefix = kind.upper()
        if pd.isna(present):
            return None, f"{prefix}_MISSING"
        suspended = _suspension_state(suspension)
        if suspended is None:
            return None, f"{prefix}_INVALID_SUSPENSION"
        if suspended:
            return None, f"{prefix}_SUSPENDED"

        price = _positive_finite(source_price)
        if price is None:
            return None, f"{prefix}_INVALID_PRICE"
        if not use_factor:
            return price, None

        factor_value = _positive_finite(factor)
        if factor_value is None:
            return None, f"{prefix}_INVALID_ADJUSTMENT_FACTOR"
        raw_price = price / factor_value
        if not math.isfinite(raw_price) or raw_price <= 0:
            return None, f"{prefix}_INVALID_PRICE"
        return raw_price, None

    def build(self, *, cache: CacheView) -> None:
        scores = cache.history(Topic.SIGNAL_SCORES)
        if scores.empty:
            cache.publish(Topic.EVALUATION_LABELS, None, pd.DataFrame(columns=LABEL_COLUMNS))
            return

        context = cache.read(Topic.RUN_CONTEXT)
        basis = context.label_price_basis
        keys = scores.loc[:, ["date", "code"]].reset_index(drop=True)
        first_signal_month = _signal_month(keys["date"].min())
        last_signal_month = _signal_month(keys["date"].max())

        files = self._monthly_files()
        latest_month = max(files)
        if last_signal_month > latest_month:
            raise ValueError("signal date is beyond the available MarketData dataset")

        calendar = []
        current_month = first_signal_month
        while True:
            path = files.get(current_month)
            if path is None:
                raise FileNotFoundError(
                    f"missing MarketData month {current_month[0]}{current_month[1]:02d} "
                    "inside the required label calendar"
                )
            month_dates = self._read_calendar_month(path, current_month)
            if calendar and month_dates and month_dates[0] <= calendar[-1]:
                raise ValueError("MarketData trading dates are not strictly increasing by month")
            calendar.extend(month_dates)

            if current_month >= last_signal_month:
                positions = {day: index for index, day in enumerate(calendar)}
                last_signal_date = keys["date"].max()
                if last_signal_date not in positions:
                    raise ValueError(f"signal date {last_signal_date} is absent from MarketData calendar")
                enough_days = (
                    positions[last_signal_date] + self.config.holding_period + 1 < len(calendar)
                )
                if enough_days or current_month == latest_month:
                    break
            if current_month == latest_month:
                break
            current_month = _next_month(current_month)

        positions = {day: index for index, day in enumerate(calendar)}
        absent_signals = sorted(set(keys["date"]) - positions.keys())
        if absent_signals:
            raise ValueError(f"signal dates are absent from MarketData calendar: {absent_signals[:3]}")

        signal_positions = keys["date"].map(positions).to_numpy()
        entry_positions = signal_positions + 1
        exit_positions = signal_positions + self.config.holding_period + 1
        entry_dates = [calendar[index] if index < len(calendar) else None for index in entry_positions]
        exit_dates = [calendar[index] if index < len(calendar) else None for index in exit_positions]

        labels = keys
        labels["entry_date"] = entry_dates
        labels["exit_date"] = exit_dates
        labels["row_index"] = range(len(labels))
        missing_reasons = [
            "TAIL" if entry is None or exit_ is None else None
            for entry, exit_ in zip(entry_dates, exit_dates)
        ]
        returns = [None] * len(labels)

        usable = labels.loc[pd.isna(missing_reasons), ["row_index", "code", "entry_date", "exit_date"]]
        endpoints = pd.concat(
            [
                usable.loc[:, ["row_index", "code", "entry_date"]]
                .rename(columns={"entry_date": "date"})
                .assign(endpoint="entry"),
                usable.loc[:, ["row_index", "code", "exit_date"]]
                .rename(columns={"exit_date": "date"})
                .assign(endpoint="exit"),
            ],
            ignore_index=True,
        )
        if not endpoints.empty:
            prices = self._read_endpoint_prices(endpoints, files, basis, context.data_capabilities)
            joined = endpoints.merge(
                prices, on=["date", "code"], how="left", sort=False, validate="many_to_one",
            )
            entry_values, exit_values = [None] * len(labels), [None] * len(labels)
            entry_issues, exit_issues = [None] * len(labels), [None] * len(labels)
            factor_mode = basis == "raw_open" and not context.data_capabilities.raw_prices
            value_columns = ["row_index", "endpoint", "present", "is_suspend", "source_price"]
            if factor_mode:
                value_columns.append("factor")
            for row in joined[value_columns].itertuples(index=False, name=None):
                row_index, endpoint, present, suspension, source_price = row[:5]
                factor = row[5] if factor_mode else None
                value, issue = self._endpoint_value(
                    present, suspension, source_price, factor,
                    kind=endpoint, use_factor=factor_mode,
                )
                target_values, target_issues = (
                    (entry_values, entry_issues) if endpoint == "entry"
                    else (exit_values, exit_issues)
                )
                target_values[row_index], target_issues[row_index] = value, issue

            for index in range(len(labels)):
                if missing_reasons[index] == "TAIL":
                    continue
                missing_reasons[index] = entry_issues[index] or exit_issues[index]
                if missing_reasons[index] is None:
                    future_return = exit_values[index] / entry_values[index] - 1.0
                    if math.isfinite(future_return):
                        returns[index] = future_return
                    else:
                        missing_reasons[index] = "INVALID_RETURN"

        output = pd.DataFrame(
            {
                "date": labels["date"],
                "code": labels["code"],
                "entry_date": entry_dates,
                "exit_date": exit_dates,
                "future_return": returns,
                "label_price_basis": basis,
                "missing_reason": missing_reasons,
            },
            columns=LABEL_COLUMNS,
        )
        cache.publish(Topic.EVALUATION_LABELS, None, output)
