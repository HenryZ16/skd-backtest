"""Raw prediction and portfolio metrics calculated from audit tables."""

from math import fsum, isfinite, sqrt
from numbers import Real

from .contracts import Topic
from .runtime_cache import CacheView
from .schemas import METRIC_NAMES


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if isfinite(value) else None


def _values(values):
    return [number for value in values if (number := _number(value)) is not None]


def _mean(values):
    if not values:
        return None
    try:
        result = fsum(values) / len(values)
    except (OverflowError, ValueError):
        return None
    return result if isfinite(result) else None


def _sample_std(values):
    if len(values) < 2:
        return None
    mean = _mean(values)
    if mean is None:
        return None
    try:
        variance = fsum((value - mean) ** 2 for value in values) / (len(values) - 1)
    except (OverflowError, ValueError):
        return None
    if not isfinite(variance):
        return None
    deviation = sqrt(max(variance, 0.0))
    scale = max(map(abs, values))
    return 0.0 if deviation <= 1e-12 * scale else deviation


def _sum(values):
    try:
        result = fsum(_values(values))
    except (OverflowError, ValueError):
        return None
    return result if isfinite(result) else None


def _annualized_return(final_nav, days, trading_days_per_year):
    if final_nav is None or final_nav < 0 or not days:
        return None
    try:
        result = final_nav ** (trading_days_per_year / days) - 1.0
    except (OverflowError, ValueError):
        return None
    return result if isfinite(result) else None


def _maximum_drawdown(nav_values):
    values = _values(nav_values)
    if not values:
        return None
    peak = 1.0
    maximum = 0.0
    for nav in values:
        peak = max(peak, nav)
        maximum = max(maximum, (peak - nav) / peak)
    return maximum if isfinite(maximum) else None


def _plain_number(value):
    if value is None:
        return None
    if type(value) is int:
        return value
    number = _number(value)
    return number


class Metrics:
    def __init__(self, trading_days_per_year: int, risk_free_rate: float):
        if type(trading_days_per_year) is not int or trading_days_per_year < 1:
            raise ValueError("trading_days_per_year must be a positive integer")
        if (isinstance(risk_free_rate, bool) or not isinstance(risk_free_rate, Real)
                or not isfinite(risk_free_rate) or risk_free_rate <= -1):
            raise ValueError("risk_free_rate must be finite and greater than -1")
        self.trading_days_per_year = trading_days_per_year
        self.risk_free_rate = float(risk_free_rate)
        self._annualization_factor = sqrt(trading_days_per_year)
        self._daily_risk_free_rate = (1.0 + self.risk_free_rate) ** (
            1.0 / trading_days_per_year
        ) - 1.0

    def calculate(self, *, cache: CacheView) -> None:
        tables = cache.result_tables()
        rankic_table = tables["rankic"]
        equity_curve = tables["equity_curve"]
        orders = tables["orders"]
        days = len(equity_curve)

        rankic = _values(rankic_table["rankic"]) if days else []
        mean_rankic = _mean(rankic)
        rankic_std = _sample_std(rankic)
        rankic_ir = (
            _plain_number(mean_rankic / rankic_std)
            if mean_rankic is not None and rankic_std not in (None, 0.0)
            else None
        )
        positive_rankic_ratio = (
            sum(value > 0 for value in rankic) / len(rankic) if rankic else None
        )

        portfolio_returns = _values(equity_curve["portfolio_return"])
        nav_values = _values(equity_curve["portfolio_nav"])
        final_nav = _number(equity_curve["portfolio_nav"].iloc[-1]) if days else None
        total_return = _plain_number(final_nav - 1.0) if final_nav is not None else None
        annualized_return = _annualized_return(
            final_nav, days, self.trading_days_per_year
        )
        volatility = _sample_std(portfolio_returns)
        annualized_volatility = (
            _plain_number(volatility * self._annualization_factor)
            if volatility is not None else None
        )
        maximum_drawdown = _maximum_drawdown(nav_values) if days else None

        benchmark_returns = _values(equity_curve["benchmark_return"])
        benchmark_navs = _values(equity_curve["benchmark_nav"])
        has_benchmark = bool(benchmark_returns or benchmark_navs)
        annualized_excess_return = tracking_error = information_ratio = None
        if has_benchmark and days:
            benchmark_final_nav = _number(equity_curve["benchmark_nav"].iloc[-1])
            benchmark_annualized_return = _annualized_return(
                benchmark_final_nav, days, self.trading_days_per_year
            )
            if annualized_return is not None and benchmark_annualized_return is not None:
                annualized_excess_return = _plain_number(
                    annualized_return - benchmark_annualized_return
                )

            active_returns = _values(equity_curve["active_return"])
            active_mean = _mean(active_returns)
            active_std = _sample_std(active_returns)
            if active_std is not None:
                tracking_error = _plain_number(active_std * self._annualization_factor)
                if active_mean is not None and active_std != 0.0:
                    information_ratio = _plain_number(
                        active_mean / active_std * self._annualization_factor
                    )

        sharpe_ratio = None
        if days:
            excess_returns = [
                value - self._daily_risk_free_rate for value in portfolio_returns
            ]
            excess_mean = _mean(excess_returns)
            excess_std = _sample_std(excess_returns)
            if excess_mean is not None and excess_std not in (None, 0.0):
                sharpe_ratio = _plain_number(
                    excess_mean / excess_std * self._annualization_factor
                )

        failed_orders = (
            sum(isinstance(status, str) and status == "REJECTED"
                for status in orders["status"])
            if days else 0
        )
        metrics = {
            "mean_rankic": _plain_number(mean_rankic),
            "rankic_std": _plain_number(rankic_std),
            "rankic_ir": rankic_ir,
            "positive_rankic_ratio": _plain_number(positive_rankic_ratio),
            "total_return": total_return,
            "annualized_return": _plain_number(annualized_return),
            "annualized_excess_return": annualized_excess_return,
            "annualized_volatility": annualized_volatility,
            "maximum_drawdown": _plain_number(maximum_drawdown),
            "tracking_error": tracking_error,
            "information_ratio": information_ratio,
            "sharpe_ratio": sharpe_ratio,
            "turnover": _sum(equity_curve["turnover"]) if days else 0.0,
            "transaction_cost": _sum(equity_curve["transaction_cost"]) if days else 0.0,
            "failed_orders": int(failed_orders),
        }
        cache.publish(
            Topic.EVALUATION_METRICS,
            None,
            {name: _plain_number(metrics[name]) for name in METRIC_NAMES},
        )
