"""Call the participant with research data only, then publish validated scores."""

from collections.abc import Callable
from contextlib import contextmanager
import random
from threading import Lock

import numpy as np
from math import isfinite
from numbers import Real

import pandas as pd

from .contracts import Topic
from .runtime_cache import CacheView
from .schemas import SCORE_COLUMNS

Inference = Callable[..., pd.DataFrame]
_RANDOM_LOCK = Lock()


def _random_states(seed):
    return [random.Random(seed).getstate(), np.random.RandomState(seed).get_state()]


@contextmanager
def _random_scope(states):
    # Own the participant's legacy Python/NumPy RNG streams without leaving
    # process-global RNGs changed after loading or invoking the model.
    with _RANDOM_LOCK:
        previous = random.getstate(), np.random.get_state()
        random.setstate(states[0])
        np.random.set_state(states[1])
        try:
            yield
        finally:
            states[:] = random.getstate(), np.random.get_state()
            random.setstate(previous[0])
            np.random.set_state(previous[1])


class SubmissionRunner:
    def __init__(self, inference: Inference, random_seed: int = 0):
        if not callable(inference):
            raise TypeError("inference must be callable")
        self.inference = inference
        self.random_seed = random_seed
        self.reset_random_state()

    def reset_random_state(self):
        self._random_states = _random_states(self.random_seed)

    @classmethod
    def from_submission(cls, submission_dir, random_seed: int = 0):
        """Load one submission and initialize its model exactly once.

        The inference file is loaded as a private package so relative helper
        imports stay separate between teams. Trained artifacts are only passed
        to the participant's constructor; the platform never fits a model.
        """
        import importlib.util
        from pathlib import Path
        import sys
        from uuid import uuid4

        directory = Path(submission_dir).resolve(strict=True)
        source = directory / "inference.py"
        model_dir = directory / "model"
        if not source.is_file() or not model_dir.is_dir():
            raise ValueError("submission must contain inference.py and a model directory")
        name = "_skd_submission_" + uuid4().hex
        spec = importlib.util.spec_from_file_location(
            name, source, submodule_search_locations=[str(directory)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        states = _random_states(random_seed)
        try:
            with _random_scope(states):
                spec.loader.exec_module(module)
                model = module.InferenceModel(str(model_dir))
            runner = cls(model.predict, random_seed)
            runner._random_states = states
        except BaseException:
            for key in list(sys.modules):
                if key == name or key.startswith(name + "."):
                    del sys.modules[key]
            raise
        runner.model = model
        runner.submission_module = module
        return runner

    def predict(self, *, as_of_date: str, data: dict[str, pd.DataFrame], cache: CacheView) -> None:
        # Capture membership before participant code can mutate its research copy.
        universe = set(cache.read(Topic.REFERENCE_PORTFOLIO, as_of_date).universe.code)
        scores = self.infer(
            as_of_date=as_of_date, data=data, universe=universe,
            check_schema=as_of_date == cache.read(Topic.RUN_CALENDAR).trading_dates[0],
        )
        self.publish_scores(as_of_date=as_of_date, scores=scores, cache=cache)

    def infer(self, *, as_of_date: str, data: dict[str, pd.DataFrame],
              universe: frozenset | set, check_schema: bool) -> pd.DataFrame:
        """Invoke and validate the model without accessing the runtime cache."""
        with _random_scope(self._random_states):
            scores = self.inference(as_of_date=as_of_date, data=data)
        # Schema is fixed for the run; changing row values still need validation each signal.
        if check_schema:
            if not isinstance(scores, pd.DataFrame) or not scores.columns.is_unique or not set(SCORE_COLUMNS).issubset(scores.columns):
                raise ValueError("inference must return a DataFrame with date/code/score")
        scores = scores.loc[:, SCORE_COLUMNS]
        if not scores.date.eq(as_of_date).all():
            raise ValueError("score dates must match as_of_date")
        if scores.code.isna().any() or scores.code.duplicated().any() or set(scores.code) != universe:
            raise ValueError("scores must cover the legal universe exactly once")
        if not all(isinstance(value, Real) and not isinstance(value, bool) and isfinite(value) for value in scores.score):
            raise ValueError("scores must be finite numeric values")
        return scores.sort_values("code", kind="stable", ignore_index=True)

    def publish_scores(self, *, as_of_date: str, scores: pd.DataFrame, cache: CacheView) -> None:
        """Publish only when the owning thread reaches this date's SIGNAL phase."""
        cache.publish(Topic.SIGNAL_SCORES, as_of_date, scores)
