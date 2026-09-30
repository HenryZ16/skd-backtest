"""Daily market playback and shared component protocol."""

from importlib.metadata import version as _version

from .config import CostConfig, FeeScheduleEntry, OptimizerConfig
from .engine import BacktestEngine

__version__ = _version("skd-backtest")

__all__ = [
    "BacktestEngine", "CostConfig", "FeeScheduleEntry",
    "OptimizerConfig", "__version__",
]
