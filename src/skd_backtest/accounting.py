"""Daily portfolio valuation and audit snapshots."""

from math import isfinite

import pandas as pd

from .config import BacktestConfig
from .contracts import BenchmarkDay, CloseSnapshot, OpenSnapshot, Topic
from .runtime_cache import CacheView
from .schemas import RESULT_COLUMNS, STATE_COLUMNS, VALUE_COLUMNS, WEIGHT_COLUMNS


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


def _date_key(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    digits = "".join(char for char in str(value) if char.isdigit())
    return int(digits[:8]) if len(digits) >= 8 else None


def _date_text(value):
    key = _date_key(value)
    if key is None:
        return str(value)
    return f"{key // 10000:04d}-{key // 100 % 100:02d}-{key % 100:02d}"


def _flag(value):
    try:
        return False if pd.isna(value) else bool(value)
    except (TypeError, ValueError):
        return False


def _market_rows(market, held_codes):
    if market.empty:
        return {}
    # Only held securities need Python records for valuation.
    held_market = market.loc[market["code"].isin(held_codes)]
    return held_market.set_index("code").to_dict("index")


def _historical_prices(row, price_mode, today):
    if row is None:
        return []
    if price_mode == "raw_price":
        raw_fields = ("raw_reference_close", "raw_previous_close")
        if any(name in row for name in raw_fields):
            pairs = ((raw_fields[0], "raw_reference_date"),
                     (raw_fields[1], "raw_previous_close_date"))
        elif not any(name in row for name in ("adjusted_open", "adjusted_close")):
            pairs = (("reference_close", "reference_date"),
                     ("previous_close", "previous_close_date"))
        else:
            pairs = ()
    else:
        pairs = (("reference_close", "reference_date"),
                 ("previous_close", "previous_close_date"))

    result = []
    for price_column, date_column in pairs:
        price = _number(row.get(price_column))
        price_date = row.get(date_column)
        price_day = _date_key(price_date)
        if price is not None and price > 0 and price_day is not None and price_day < today:
            result.append((price_day, 0, price, _date_text(price_date)))
    return result


def _valuation_price(position, market_row, *, code, date, price_mode, price_column):
    today = _date_key(date)
    if market_row is not None and not _flag(market_row.get("is_suspended")) \
            and not _flag(market_row.get("is_missing")):
        price = _number(market_row.get(price_column))
        if price is not None and price > 0:
            return price, date

    account_price = _number(position.get("reference_price"))
    account_date = position.get("reference_date")
    account_day = _date_key(account_date)
    if account_price is not None and account_price > 0 and account_day is not None \
            and account_day <= today:
        if account_day == today:
            return account_price, _date_text(account_date)
        candidates = [(account_day, 1, account_price, _date_text(account_date))]
    else:
        candidates = []

    candidates.extend(_historical_prices(market_row, price_mode, today))
    if not candidates:
        raise ValueError(
            f"cannot value {price_mode} holding {code!r} on {date}: "
            "no valid current or historical reference price"
        )
    _, _, price, price_date = max(candidates, key=lambda item: (item[0], item[1]))
    return price, price_date


def _mark_account(account, market, *, date, stage):
    mode = account.price_mode
    if account.positions.empty:
        return account.positions, pd.DataFrame(columns=VALUE_COLUMNS), 0.0
    price_column = ("raw_open" if stage == "open" else "raw_close") \
        if mode == "raw_price" else ("adjusted_open" if stage == "open" else "adjusted_close")
    market_by_code = _market_rows(market, account.positions.code)
    marked_rows = []
    values = []
    market_value = 0.0
    changed = False

    for original in account.positions.to_dict("records"):
        row = original
        code = row["code"]
        if mode == "raw_price":
            shares = _number(row.get("total_shares"))
            if shares is None or shares < 0:
                raise ValueError(f"invalid raw share count for {code!r}")
            if shares == 0:
                marked_rows.append(row)
                continue
        else:
            position_value = _number(row.get("position_value"))
            reference_price = _number(row.get("reference_price"))
            if position_value is None or position_value < 0:
                raise ValueError(f"invalid adjusted position value for {code!r}")
            if position_value == 0:
                marked_rows.append(row)
                continue
            if reference_price is None or reference_price <= 0:
                raise ValueError(f"adjusted holding {code!r} has no valid reference price")

        price, price_date = _valuation_price(
            row, market_by_code.get(code), code=code, date=date,
            price_mode=mode, price_column=price_column,
        )
        if mode == "raw_price":
            value = shares * price
            update = price != _number(row.get("reference_price")) \
                or price_date != _date_text(row.get("reference_date"))
        else:
            value = position_value * price / reference_price
            update = value != position_value or price != reference_price \
                or price_date != _date_text(row.get("reference_date"))
        if not isfinite(value):
            raise ValueError(f"non-finite market value for {code!r} on {date}")

        if update:
            row = dict(row, reference_price=price, reference_date=price_date)
            if mode == "adjusted_return":
                row["position_value"] = value
            changed = True
        marked_rows.append(row)
        values.append(dict(code=code, price=price, price_date=price_date, market_value=value))
        market_value += value

    if not isfinite(market_value):
        raise ValueError(f"non-finite portfolio market value on {date}")
    positions = (pd.DataFrame(marked_rows, columns=STATE_COLUMNS[mode])
                 if changed else account.positions)
    return positions, pd.DataFrame(values, columns=VALUE_COLUMNS), market_value


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
        positions, _, market_value = _mark_account(
            execution.account, market, date=date, stage="close",
        )
        account = execution.account
        if positions is not account.positions:
            account = type(account)(account.price_mode, account.cash, positions, account.locked_lots)

        position_rows = []
        for value in account.positions.to_dict("records"):
            if account.price_mode == "raw_price":
                shares = _number(value.get("total_shares")) or 0.0
                sellable = _number(value.get("sellable_shares")) or 0.0
                if shares == 0:
                    continue
                close = _number(value.get("reference_price"))
                position_value = shares * close
            else:
                position_value = _number(value.get("position_value")) or 0.0
                if position_value == 0:
                    continue
                shares = sellable = None
                close = _number(value.get("reference_price"))
            position_rows.append(dict(
                date=date, code=value["code"], shares=shares, sellable_shares=sellable,
                close=close, market_value=position_value,
                weight=position_value / (execution.account.cash + market_value),
            ))
        positions_table = pd.DataFrame(position_rows, columns=RESULT_COLUMNS["positions"])
        if not positions_table.empty:
            positions_table = positions_table.sort_values("code", kind="stable", ignore_index=True)
        weights = (positions_table.loc[:, ["code", "weight"]]
                   if not positions_table.empty else pd.DataFrame(columns=WEIGHT_COLUMNS))

        portfolio_value = execution.account.cash + market_value
        if not isfinite(portfolio_value) or portfolio_value < 0:
            raise ValueError(f"invalid portfolio value on {date}")
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
        benchmark_return = benchmark_nav = active_return = None
        if self.config.benchmark_mode != "none":
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
