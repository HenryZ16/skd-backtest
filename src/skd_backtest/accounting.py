"""Daily portfolio valuation and audit snapshots."""

from math import isfinite

import numpy as np
import pandas as pd

from .config import BacktestConfig
from .contracts import BenchmarkDay, CloseSnapshot, OpenSnapshot, Topic
from .runtime_cache import CacheView
from .schemas import RESULT_COLUMNS, VALUE_COLUMNS, WEIGHT_COLUMNS


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        if pd.isna(value):
            return None
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) else None


def _numeric_column(frame, name):
    if name not in frame:
        return np.full(len(frame), np.nan)
    column = frame[name]
    if column.dtype.kind not in "iuf":
        column = column.map(_number)
    return column.to_numpy(dtype=float, na_value=np.nan)


def _date_column(frame, name):
    if name not in frame:
        return np.full(len(frame), np.nan)
    column = frame[name]
    if column.dtype.kind in "iuf":
        days = column.to_numpy(dtype=float, na_value=np.nan)
        return np.where((days >= 10_000_000) & (days < 100_000_000), np.trunc(days), np.nan)
    digits = column.astype("string").str.replace(r"\D", "", regex=True).str[:8]
    return pd.to_numeric(digits.where(digits.str.len() == 8), errors="coerce").to_numpy(
        dtype=float, na_value=np.nan)


def _valuation_prices(positions, market, *, date, price_mode, price_column):
    prices = _numeric_column(market, price_column).copy()
    valid = np.isfinite(prices) & (prices > 0)
    for name in ("is_suspended", "is_missing"):
        if name in market:
            flags = market[name].to_numpy(dtype=bool, na_value=False)
            valid &= ~flags
    price_dates = np.full(len(positions), date, dtype=object)
    if valid.all():
        return prices, price_dates

    # Select fallback sources for every unavailable price with the same masks.
    missing = ~valid
    held = positions.iloc[np.flatnonzero(missing)]
    history = market.iloc[np.flatnonzero(missing)]
    today = int(date.replace("-", ""))
    chosen_prices = _numeric_column(held, "reference_price").copy()
    chosen_dates = _date_column(held, "reference_date")
    available = (np.isfinite(chosen_prices) & (chosen_prices > 0)
                 & np.isfinite(chosen_dates) & (chosen_dates <= today))
    chosen_prices[~available] = np.nan
    chosen_dates = np.where(available, chosen_dates, -np.inf)

    pairs = (("reference_close", "reference_date"), ("previous_close", "previous_close_date"))
    if price_mode == "raw_price":
        if any(name in market for name in ("raw_reference_close", "raw_previous_close")):
            pairs = (("raw_reference_close", "raw_reference_date"),
                     ("raw_previous_close", "raw_previous_close_date"))
        elif any(name in market for name in ("adjusted_open", "adjusted_close")):
            pairs = ()
    for price_name, date_name in pairs:
        candidate_prices = _numeric_column(history, price_name)
        candidate_dates = _date_column(history, date_name)
        newer = (np.isfinite(candidate_prices) & (candidate_prices > 0)
                 & np.isfinite(candidate_dates) & (candidate_dates < today)
                 & (candidate_dates > chosen_dates))
        # Strictly newer preserves account > reference close > previous close on ties.
        chosen_prices = np.where(newer, candidate_prices, chosen_prices)
        chosen_dates = np.where(newer, candidate_dates, chosen_dates)
    invalid = ~np.isfinite(chosen_prices)
    if invalid.any():
        code = held.code.iloc[np.flatnonzero(invalid)[0]]
        raise ValueError(f"cannot value {price_mode} holding {code!r} on {date}: "
                         "no valid current or historical reference price")
    prices[missing] = chosen_prices
    price_dates[missing] = pd.to_datetime(chosen_dates.astype(np.int64), format="%Y%m%d").strftime("%Y-%m-%d")
    return prices, price_dates


def _mark_account(account, market, *, date, stage):
    mode = account.price_mode
    source = account.positions
    if source.empty:
        return source, pd.DataFrame(columns=VALUE_COLUMNS), 0.0
    codes = source.code.to_numpy()
    amount_column = "total_shares" if mode == "raw_price" else "position_value"
    amounts = _numeric_column(source, amount_column)
    invalid = ~np.isfinite(amounts) | (amounts < 0)
    if invalid.any():
        name = "raw share count" if mode == "raw_price" else "adjusted position value"
        raise ValueError(f"invalid {name} for {codes[np.flatnonzero(invalid)[0]]!r}")
    active = amounts > 0
    if not active.any():
        return source, pd.DataFrame(columns=VALUE_COLUMNS), 0.0
    references = _numeric_column(source, "reference_price")
    if mode == "adjusted_return":
        invalid = active & (~np.isfinite(references) | (references <= 0))
        if invalid.any():
            raise ValueError(f"adjusted holding {codes[np.flatnonzero(invalid)[0]]!r} has no valid reference price")

    held = source.iloc[np.flatnonzero(active)]
    aligned = (market.set_index("code").reindex(codes[active]) if not market.empty
               else pd.DataFrame(index=range(len(held))))
    price_column = ("raw_open" if stage == "open" else "raw_close") \
        if mode == "raw_price" else ("adjusted_open" if stage == "open" else "adjusted_close")
    prices, price_dates = _valuation_prices(
        held, aligned, date=date, price_mode=mode, price_column=price_column,
    )

    with np.errstate(over="ignore", invalid="ignore"):
        values = amounts[active] * prices
        if mode == "adjusted_return":
            values = values / references[active]
    invalid = ~np.isfinite(values)
    if invalid.any():
        raise ValueError(f"non-finite market value for {codes[active][np.flatnonzero(invalid)[0]]!r} on {date}")
    # Preserve the previous left-to-right sum so cash-sensitive orders stay identical.
    with np.errstate(over="ignore"):
        market_value = float(np.add.accumulate(values)[-1])
    if not isfinite(market_value):
        raise ValueError(f"non-finite portfolio market value on {date}")

    dates = source.reference_date.to_numpy(dtype=object, na_value=None)
    changed = (np.any(prices != references[active]) or np.any(price_dates != dates[active])
               or (mode == "adjusted_return" and np.any(values != amounts[active])))
    positions = source
    if changed:
        positions = source.copy()
        positions.index = pd.RangeIndex(len(positions))
        updated_prices = source.reference_price.to_numpy(copy=True)
        if updated_prices.dtype.kind != "f":
            updated_prices = updated_prices.astype(float if updated_prices.dtype.kind in "iu" else object)
        updated_prices[active] = prices
        updated_dates = dates.copy()
        updated_dates[active] = price_dates
        positions["reference_price"] = updated_prices
        positions["reference_date"] = updated_dates
        if mode == "adjusted_return":
            updated_values = amounts.copy()
            updated_values[active] = values
            positions["position_value"] = updated_values
    return positions, pd.DataFrame({
        "code": codes[active], "price": prices, "price_date": price_dates, "market_value": values,
    }, columns=VALUE_COLUMNS), market_value


class PortfolioAccounting:
    def __init__(self, config: BacktestConfig):
        self.config = config

    def mark_at_open(self, *, date: str, market: pd.DataFrame, cache: CacheView) -> None:
        account = cache.read(Topic.ACCOUNT_SETTLED, date)
        positions, values, market_value = _mark_account(account, market, date=date, stage="open")
        if positions is not account.positions:
            account = type(account)(account.price_mode, account.cash, positions, account.locked_lots)
        cache.publish(Topic.ACCOUNT_OPEN, date, OpenSnapshot(
            date, account, values, market_value, account.cash + market_value,
        ))

    def mark_to_market(self, *, date: str, market: pd.DataFrame, cache: CacheView) -> None:
        execution = cache.read(Topic.EXECUTION_DAY, date)
        opening = cache.read(Topic.ACCOUNT_OPEN, date)
        positions, values, market_value = _mark_account(
            execution.account, market, date=date, stage="close",
        )
        account = execution.account
        if positions is not account.positions:
            account = type(account)(account.price_mode, account.cash, positions, account.locked_lots)

        portfolio_value = execution.account.cash + market_value
        if not isfinite(portfolio_value) or portfolio_value < 0:
            raise ValueError(f"invalid portfolio value on {date}")
        if values.empty:
            positions_table = pd.DataFrame(columns=RESULT_COLUMNS["positions"])
        else:
            raw = account.price_mode == "raw_price"
            live = np.ones(len(values), dtype=bool) if raw else values.market_value.to_numpy() > 0
            shares = sellable = None
            if raw:
                held_shares = _numeric_column(positions, "total_shares")
                shares = held_shares[held_shares > 0]
                sellable = _numeric_column(positions, "sellable_shares")[held_shares > 0]
                sellable = np.where(np.isfinite(sellable), sellable, 0.0)
            positions_table = pd.DataFrame({
                "date": date, "code": values.code.to_numpy()[live],
                "shares": shares, "sellable_shares": sellable,
                "close": values.price.to_numpy()[live],
                "market_value": values.market_value.to_numpy()[live],
                "weight": values.market_value.to_numpy()[live] / portfolio_value,
            }, columns=RESULT_COLUMNS["positions"]).sort_values("code", kind="stable", ignore_index=True)
        weights = (positions_table.loc[:, ["code", "weight"]]
                   if not positions_table.empty else pd.DataFrame(columns=WEIGHT_COLUMNS))

        dates = cache.read(Topic.RUN_CALENDAR).trading_dates
        day_index = dates.index(date)
        if day_index:
            previous_row = cache.read(Topic.ACCOUNT_CLOSE, dates[day_index - 1]).equity_curve.iloc[0]
            previous_value = _number(previous_row["portfolio_value"])
            previous_benchmark_nav = _number(previous_row["benchmark_nav"])
        else:
            initial = cache.read(Topic.ACCOUNT_INITIAL)
            previous_value = _number(initial.portfolio_value)
            previous_benchmark_nav = _number(initial.benchmark_nav)
        if previous_value is None or previous_value <= 0:
            raise ValueError(f"cannot calculate portfolio return on {date}: invalid prior equity")
        portfolio_return = portfolio_value / previous_value - 1.0

        benchmark_data = cache.read(Topic.REFERENCE_BENCHMARK, date)
        if benchmark_data.status != "available":
            raise ValueError(f"benchmark data unavailable on {date}: {benchmark_data.reason}")
        benchmark = benchmark_data.data
        if not isinstance(benchmark, BenchmarkDay) or benchmark.date != date:
            raise ValueError(f"invalid benchmark data on {date}")
        benchmark_return = _number(benchmark.benchmark_return)
        if benchmark_return is None or benchmark_return < -1:
            raise ValueError(f"invalid benchmark return on {date}")
        if previous_benchmark_nav is None or previous_benchmark_nav < 0:
            raise ValueError(f"missing prior benchmark NAV on {date}")
        benchmark_nav = previous_benchmark_nav * (1.0 + benchmark_return)
        if not isfinite(benchmark_nav):
            raise ValueError(f"invalid benchmark NAV on {date}")
        active_return = portfolio_return - benchmark_return

        turnover = _number(execution.trade_value)
        opening_value = _number(opening.portfolio_value)
        transaction_cost = _number(execution.total_cost)
        if turnover is None or turnover < 0 or transaction_cost is None or transaction_cost < 0:
            raise ValueError(f"invalid execution totals on {date}")
        if opening_value is None or opening_value <= 0:
            if turnover:
                raise ValueError(f"cannot calculate turnover on {date}: invalid opening equity")
            turnover = 0.0
        else:
            turnover /= opening_value

        equity_row = dict(
            date=date, cash=execution.account.cash, market_value=market_value,
            portfolio_value=portfolio_value, portfolio_nav=portfolio_value / self.config.initial_cash,
            portfolio_return=portfolio_return, benchmark_nav=benchmark_nav,
            benchmark_return=benchmark_return, active_return=active_return,
            turnover=turnover, transaction_cost=transaction_cost,
        )
        cache.publish(Topic.ACCOUNT_CLOSE, date, CloseSnapshot(
            date, account, positions_table, pd.DataFrame([equity_row], columns=RESULT_COLUMNS["equity_curve"]),
            weights,
        ))
