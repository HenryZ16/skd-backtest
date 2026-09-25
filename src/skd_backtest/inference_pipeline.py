"""Bounded lookahead for a single, chronological model worker."""

from collections import deque
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import closing
from threading import Event
from time import perf_counter


def inference_days(days, runner, *, rebalance_interval, enabled):
    """Yield daily market packets and their optional prediction futures.

    The calling thread alone advances the market iterator. At most one rebalance
    interval of future days is buffered; model calls run in submission order.
    """
    with closing(days):
        if not enabled:
            for day in days:
                yield day, None
            return

        stopped = Event()
        pending = deque()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="skd-infer")

        def infer(date, research, universe, check_schema):
            if stopped.is_set():
                raise CancelledError("inference pipeline stopped")
            started = perf_counter()
            try:
                scores = runner.infer(as_of_date=date, data=research, universe=universe,
                                      check_schema=check_schema)
                return scores, perf_counter() - started
            except BaseException:
                stopped.set()
                raise

        first_signal = True
        try:
            for day in days:
                prediction = None
                if day.research is not None:
                    # Capture the legal pool independently of the participant's mutable inputs.
                    universe = frozenset(day.portfolio["barra_exposures"]["代码"])
                    prediction = executor.submit(infer, day.date, day.research, universe, first_signal)
                    first_signal = False
                    # Transfer the existing research packet; do not pin it in the daily queue.
                    day.research = None
                pending.append((day, prediction))
                if len(pending) >= rebalance_interval + 1:
                    yield pending.popleft()
            while pending:
                yield pending.popleft()
        finally:
            stopped.set()
            # A running user callback must return before teardown; queued work is cancelled.
            executor.shutdown(wait=True, cancel_futures=True)
            pending.clear()
