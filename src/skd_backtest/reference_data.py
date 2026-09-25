"""Reference-data entry points; external source adapters remain unimplemented."""

from .config import BacktestConfig
from .contracts import Dataset, PortfolioInputs, Topic
from .runtime_cache import CacheView


class ReferenceDataProvider:
    def __init__(self, config: BacktestConfig):
        self.config = config

    def _external(self, name):
        if getattr(self.config.reference_sources, name) is not None or getattr(self.config.data_capabilities, name):
            raise NotImplementedError(f"ReferenceDataProvider: {name} source adapter is not implemented")
        return Dataset("unavailable", None, f"{name} source is not configured")

    def prepare_open(self, *, date: str, cache: CacheView) -> None:
        data = (Dataset("unavailable", None, "adjusted_return includes corporate actions in prices")
                if self.config.price_mode == "adjusted_return" else self._external("corporate_actions"))
        cache.publish(Topic.REFERENCE_ACTIONS, date, data)

    def prepare_close(self, *, date: str, cache: CacheView) -> None:
        if self.config.benchmark_mode == "none":
            data = Dataset("unavailable", None, "benchmark_mode=none")
        else:
            data = self._external("benchmark_returns")
        cache.publish(Topic.REFERENCE_BENCHMARK, date, data)

    def prepare_signal(self, *, date: str, cache: CacheView) -> None:
        market = cache.read(Topic.MARKET_CONTEXT, date)
        cache.publish(Topic.REFERENCE_PORTFOLIO, date, PortfolioInputs(
            date, market.universe, Dataset("available", market.barra_exposures),
            self._external("benchmark_weights"), self._external("industries"),
        ))
