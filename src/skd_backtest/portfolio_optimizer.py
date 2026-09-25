"""Score-to-weight construction, independent of execution and accounting."""

from math import isclose, isfinite
from numbers import Real

import pandas as pd

from .config import OptimizerConfig
from .contracts import TargetPlan, Topic
from .runtime_cache import CacheView
from .schemas import RESULT_COLUMNS


class PortfolioOptimizer:
    def __init__(self, config: OptimizerConfig):
        self.config = config
        self._validate_config()

    def optimize(self, *, signal_date: str, cache: CacheView) -> None:
        scores = cache.read(Topic.SIGNAL_SCORES, signal_date)
        inputs = cache.read(Topic.REFERENCE_PORTFOLIO, signal_date)
        closing = cache.read(Topic.ACCOUNT_CLOSE, signal_date)
        calendar = cache.read(Topic.RUN_CALENDAR).signal_calendar
        context = cache.read(Topic.RUN_CONTEXT)

        signal_rows = calendar.loc[calendar.signal_date == signal_date, "execution_date"]
        if len(signal_rows) != 1:
            raise ValueError(f"signal date is not scheduled exactly once: {signal_date}")
        execution_date = signal_rows.iloc[0]
        if inputs.date != signal_date:
            raise ValueError("portfolio inputs must belong to the signal date")

        legal_codes = list(inputs.universe.code)
        score_map, ranked_codes = self._ranked_scores(scores)
        benchmark_map = None
        if context.backtest.benchmark_mode == "csi300":
            benchmark_map = self._benchmark_weights(
                inputs.benchmark_weights, signal_date, legal_codes,
                required=self.config.method in ("benchmark_tilt", "barra"),
            )
        elif self.config.method in ("benchmark_tilt", "barra"):
            raise ValueError("benchmark-relative optimization requires benchmark_mode=csi300")

        if self.config.method == "barra":
            from .risk_optimizer import optimize_barra
            percentiles = self._percentiles(ranked_codes, score_map)
            candidate = optimize_barra(
                config=self.config, date=signal_date, codes=sorted(legal_codes),
                alpha=percentiles, benchmark=benchmark_map, inputs=inputs,
                current_weights=closing.actual_weights,
            )
        elif self.config.method == "top_k":
            selected = ranked_codes[: min(self.config.top_k, len(ranked_codes))]
            candidate = {code: 0.0 for code in legal_codes}
            if selected:
                weight = 1.0 / len(selected)
                candidate.update(dict.fromkeys(selected, weight))
        else:
            if benchmark_map is None:
                raise ValueError("benchmark_tilt requires available historical benchmark weights")
            percentiles = self._percentiles(ranked_codes, score_map)
            candidate = {
                code: benchmark_map[code] * (0.5 + percentiles[code])
                for code in legal_codes
            }
            total = sum(candidate.values())
            if not isfinite(total) or total <= 0:
                raise ValueError("benchmark tilt has no positive benchmark-weighted candidates")
            candidate = {code: weight / total for code, weight in candidate.items()}

        targets = candidate if self.config.method == "barra" else self._apply_name_limit(candidate)
        if self.config.fully_invested and legal_codes and not isclose(
            sum(targets.values()), 1.0, rel_tol=0.0, abs_tol=1e-10
        ):
            raise ValueError("fully invested target is infeasible under the configured constraints")
        if self.config.fully_invested and not legal_codes:
            raise ValueError("fully invested target is infeasible for an empty legal universe")

        held_codes = list(closing.actual_weights.code)
        output_codes = sorted(set(legal_codes).union(held_codes))
        rows = [
            {
                "signal_date": signal_date,
                "execution_date": execution_date,
                "code": code,
                "score": score_map.get(code),
                "benchmark_weight": (
                    benchmark_map.get(code, 0.0) if benchmark_map is not None else None
                ),
                "target_weight": targets.get(code, 0.0),
            }
            for code in output_codes
        ]
        weights = pd.DataFrame(rows, columns=RESULT_COLUMNS["target_weights"])
        cache.publish(
            Topic.SIGNAL_TARGETS,
            execution_date,
            TargetPlan(signal_date, execution_date, weights),
        )
        cache.log(
            level="INFO",
            message="portfolio targets constructed",
            details={
                "method": self.config.method,
                "signal_date": signal_date,
                "execution_date": execution_date,
                "universe_size": len(legal_codes),
                "held_outside_universe": len(set(held_codes) - set(legal_codes)),
            },
        )

    def _validate_config(self) -> None:
        if self.config.method not in ("top_k", "benchmark_tilt", "barra"):
            raise NotImplementedError(f"optimizer method is not supported: {self.config.method}")
        if not self.config.long_only:
            raise NotImplementedError("short selling is not supported by the cash-equity execution engine")
        if type(self.config.fully_invested) is not bool:
            raise ValueError("fully_invested must be a boolean")
        if self.config.method == "top_k":
            if type(self.config.top_k) is not int or self.config.top_k < 1:
                raise ValueError("top_k must be a positive integer")
        for name in (
            "active_weight_limit",
            "industry_exposure_limit",
            "barra_style_exposure_limit",
            "turnover_limit",
        ):
            value = getattr(self.config, name)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value) or value < 0:
                    raise ValueError(f"{name} must be finite and nonnegative")
                if self.config.method != "barra":
                    raise ValueError(f"{name} requires method=barra")
        if self.config.method == "barra":
            risk = self.config.risk_aversion
            if isinstance(risk, bool) or not isinstance(risk, Real) or not isfinite(risk) or risk <= 0:
                raise ValueError("risk_aversion must be finite and positive")
            factors = self.config.barra_factors
            if not factors or len(set(factors)) != len(factors) or any(
                not isinstance(name, str) or not name for name in factors
            ):
                raise ValueError("barra_factors must be unique nonempty names")
        cap = self.config.single_name_weight_limit
        if cap is not None and (
            isinstance(cap, bool) or not isinstance(cap, Real)
            or not isfinite(cap) or not 0 < cap <= 1
        ):
            raise ValueError("single_name_weight_limit must be in (0, 1]")

    @staticmethod
    def _ranked_scores(scores: pd.DataFrame) -> tuple[dict, list[str]]:
        score_map = dict(zip(scores.code, scores.score))
        ranked_codes = sorted(
            score_map,
            key=lambda code: (-float(score_map[code]), code),
        )
        return score_map, ranked_codes

    @staticmethod
    def _percentiles(ranked_codes: list[str], score_map: dict) -> dict[str, float]:
        count = len(ranked_codes)
        result = {}
        start = 0
        while start < count:
            end = start + 1
            value = float(score_map[ranked_codes[start]])
            while end < count and float(score_map[ranked_codes[end]]) == value:
                end += 1
            if count == 1:
                percentile = 0.5
            else:
                average_rank = (start + end - 1) / 2
                percentile = (count - 1 - average_rank) / (count - 1)
            for code in ranked_codes[start:end]:
                result[code] = percentile
            start = end
        return result

    @staticmethod
    def _benchmark_weights(dataset, signal_date: str, legal_codes: list[str], *, required: bool):
        if dataset.status == "unavailable":
            if required:
                raise ValueError(f"benchmark weights are unavailable: {dataset.reason}")
            return None
        weights = dataset.data
        if not weights.date.eq(signal_date).all():
            raise ValueError("benchmark weights must belong to the signal date")
        if weights.code.duplicated().any() or set(weights.code) != set(legal_codes):
            raise ValueError("benchmark weights must cover the legal universe exactly once")
        values = dict(zip(weights.code, weights.benchmark_weight))
        if any(
            isinstance(value, bool) or not isinstance(value, Real)
            or not isfinite(value) or value < 0
            for value in values.values()
        ):
            raise ValueError("benchmark weights must be finite and nonnegative")
        if not isclose(sum(values.values()), 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError("historical benchmark weights must sum to one")
        return values

    def _apply_name_limit(self, candidates: dict[str, float]) -> dict[str, float]:
        cap = self.config.single_name_weight_limit
        if cap is None:
            return candidates
        if not self.config.fully_invested:
            return {code: min(weight, cap) for code, weight in candidates.items()}

        eligible = [code for code, weight in candidates.items() if weight > 0]
        if len(eligible) * cap < 1.0 - 1e-12:
            raise ValueError("single-name limit makes a fully invested target infeasible")

        result = dict.fromkeys(candidates, 0.0)
        remaining = 1.0
        uncapped = eligible
        while uncapped:
            total = sum(candidates[code] for code in uncapped)
            proposed = {
                code: remaining * candidates[code] / total
                for code in uncapped
            }
            saturated = [code for code, weight in proposed.items() if weight > cap]
            if not saturated:
                result.update(proposed)
                break
            saturated_set = set(saturated)
            for code in saturated:
                result[code] = cap
                remaining -= cap
            uncapped = [code for code in uncapped if code not in saturated_set]
        if not uncapped and remaining > 1e-10:
            raise ValueError("single-name limit makes a fully invested target infeasible")
        correction = 1.0 - sum(result.values())
        if abs(correction) > 1e-12:
            room = next(
                (code for code in eligible if result[code] + correction <= cap + 1e-12),
                None,
            )
            if room is None:
                raise ValueError("single-name limit makes a fully invested target infeasible")
            result[room] += correction
        return result
