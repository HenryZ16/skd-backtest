"""Post-run label placeholder, independent of the live market reader."""

import pandas as pd

from .config import BacktestConfig
from .contracts import Topic
from .runtime_cache import CacheView
from .schemas import LABEL_COLUMNS


class LabelProvider:
    def __init__(self, config: BacktestConfig):
        self.config = config

    def build(self, *, cache: CacheView) -> None:
        # TODO: Read evaluator-only prices and calculate Open(t+H+1) / Open(t+1) - 1.
        cache.log(level="DEBUG", message="STUB labels: future returns not calculated")
        cache.publish(Topic.EVALUATION_LABELS, None, pd.DataFrame(columns=LABEL_COLUMNS))
