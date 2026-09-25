"""Repeatable data and cache-backed engine benchmark; financial algorithms are placeholders."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging
from math import isfinite
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
from statistics import median
from threading import Event, Thread
from time import perf_counter, sleep

import pandas as pd
import psutil
import pyarrow

from skd_backtest import BacktestEngine


class MemorySampler:
    """Sample total process RSS, including native arrays and the prefetch thread."""

    def __init__(self, interval_seconds):
        if not isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("memory sampling interval must be finite and positive")
        self.interval = interval_seconds
        self.process = psutil.Process()
        self.stop = Event()
        self.thread = Thread(target=self._poll, name="benchmark-memory", daemon=True)
        self.samples = 0
        self.peak = self.area = self.max_gap = 0
        self.error = None

    def _sample(self):
        rss = self.process.memory_info().rss
        now = perf_counter()
        if self.samples:
            gap = now - self.last_time
            self.area += gap * (self.last_rss + rss) / 2
            self.max_gap = max(self.max_gap, gap)
        else:
            self.started = now
        self.last_time, self.last_rss = now, rss
        self.peak = max(self.peak, rss)
        self.samples += 1

    def _poll(self):
        try:
            while not self.stop.wait(self.interval):
                self._sample()
        except Exception as error:
            self.error = error
            self.stop.set()

    def __enter__(self):
        self._sample()
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        self.thread.join()
        if exc_type is not None:
            return False
        if self.error is not None:
            raise self.error
        self._sample()
        duration = self.last_time - self.started
        self.stats = {
            "peak_rss_bytes": self.peak,
            "mean_rss_bytes": self.area / duration if duration else self.last_rss,
            "sample_count": self.samples, "duration_seconds": duration,
            "max_sample_gap_seconds": self.max_gap,
        }


def run_isolated(args, prefetch, scope):
    # A fresh interpreter prevents earlier runs' allocator caches from inflating RSS.
    command = [sys.executable, str(Path(__file__).resolve()), "--worker",
               f"{scope}-{'async' if prefetch else 'sync'}"]
    for name in ("data_dir", "start_date", "end_date", "lookback", "rebalance_interval",
                 "read_batch_months", "inference_delay_ms", "memory_sample_ms"):
        command.extend(["--" + name.replace("_", "-"), str(getattr(args, name))])
    command.append("--async-inference" if args.async_inference else "--no-async-inference")
    result = subprocess.run(command, capture_output=True, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    if result.returncode:
        raise RuntimeError(f"benchmark worker failed ({result.returncode}):\n{result.stderr}")
    return json.loads(result.stdout)


def run_once(args, prefetch, scope):
    digest = hashlib.sha256()
    market_digest = hashlib.sha256()

    def inference(*, as_of_date, data):
        day = int(as_of_date.replace("-", ""))
        for name, table in data.items():
            if table.empty or table["日期"].max() != day or table["日期"].nunique() > args.lookback:
                raise AssertionError(f"invalid as-of window: {as_of_date} {name}")
            # Fingerprint each delivered signal-day cross-section, including all feature values.
            today = table.iloc[table["日期"].searchsorted(day):]
            digest.update(name.encode())
            digest.update(pd.util.hash_pandas_object(today, index=False).values.tobytes())
        if args.inference_delay_ms:
            sleep(args.inference_delay_ms / 1000)
        barra = data["Barra_factor"]
        codes = barra.iloc[barra["日期"].searchsorted(day):]["代码"].reset_index(drop=True)
        return pd.DataFrame({"date": as_of_date, "code": codes, "score": 0.0})

    engine = BacktestEngine(
        data_dir=args.data_dir, start_date=args.start_date, end_date=args.end_date,
        inference=inference, lookback=args.lookback, rebalance_interval=args.rebalance_interval,
        read_batch_months=args.read_batch_months, prefetch=prefetch, async_inference=args.async_inference,
    )
    if scope == "engine":
        engine.run()
        performance = engine.performance
        dates = engine.trading_dates
        predictions = len(engine.tables["predictions"])
    else:
        dates, predictions, market_rows, inference_calls = [], 0, 0, 0
        inference_seconds, consumer_seconds = 0.0, 0.0
        started = perf_counter()
        with closing(engine.data_provider.playback()) as days:
            for day in days:
                dates.append(day.date)
                consume_started = perf_counter()
                # Touch actual values, including carried prices; no finance stubs or audit tables.
                market_digest.update(day.date.encode())
                market_digest.update(struct.pack(
                    "<dddqq", float(day.open_market["adjusted_open"].sum()),
                    float(day.close_market["adjusted_close"].sum()),
                    float(day.close_market["reference_close"].sum()),
                    len(day.open_market), int(day.close_market["is_missing"].sum()),
                ))
                consumer_seconds += perf_counter() - consume_started
                market_rows += len(day.open_market)
                if day.research is not None:
                    inference_started = perf_counter()
                    predictions += len(inference(as_of_date=day.date, data=day.research))
                    inference_seconds += perf_counter() - inference_started
                    inference_calls += 1
        elapsed = perf_counter() - started
        performance = {
            "elapsed_seconds": elapsed, "playback_seconds": elapsed,
            "inference_seconds": inference_seconds, "consumer_seconds": consumer_seconds,
            "inference_calls": inference_calls, "trading_days": len(dates), "market_rows": market_rows,
            "days_per_second": len(dates) / elapsed,
            "source_rows_per_second": sum(engine.data_provider.stats["source_rows"].values()) / elapsed,
            "data": engine.data_provider.stats.copy(),
        }
    return {
        "scope": scope,
        "market_checksum": market_digest.hexdigest() if scope == "data" else None,
        "mode": "async" if prefetch else "sync",
        "signal_data_sha256": digest.hexdigest(),
        "first_trading_date": dates[0] if dates else None,
        "last_trading_date": dates[-1] if dates else None,
        "prediction_rows": predictions,
        **performance,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--start-date", default="2016-01-01")
    parser.add_argument("--end-date", default="2022-12-31")
    parser.add_argument("--lookback", type=int, default=252)
    parser.add_argument("--rebalance-interval", type=int, default=5)
    parser.add_argument("--read-batch-months", type=int, default=12)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--scope", choices=("data", "engine", "both"), default="both")
    parser.add_argument("--inference-delay-ms", type=float, default=0)
    parser.add_argument("--async-inference", action=argparse.BooleanOptionalAction, default=True,
                        help="run model inference in its own bounded worker (engine scope only)")
    parser.add_argument("--memory-sample-ms", type=float, default=10)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=("data-sync", "data-async", "engine-sync", "engine-async"),
                        help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1 or args.inference_delay_ms < 0:
        parser.error("repeats must be positive and inference-delay-ms nonnegative")
    if not isfinite(args.memory_sample_ms) or args.memory_sample_ms <= 0:
        parser.error("memory-sample-ms must be finite and positive")
    logging.getLogger("skd_backtest").setLevel(logging.WARNING)

    if args.worker:
        scope, mode = args.worker.split("-")
        with MemorySampler(args.memory_sample_ms / 1000) as memory:
            run = run_once(args, mode == "async", scope)
        run["memory"] = memory.stats
        run["process_id"] = os.getpid()
        print(json.dumps(run))
        return

    # Fingerprint the input files outside timed runs so later data changes are detectable.
    files = []
    for dataset in ("Factor33_winsor", "Barra_factor", "MarketData"):
        for path in sorted((args.data_dir / dataset).glob("*/*/*.parquet")):
            month = path.stem
            if args.start_date[:7].replace("-", "") <= month <= args.end_date[:7].replace("-", ""):
                files.append(path)
    manifest = hashlib.sha256()
    for path in files:
        manifest.update(path.relative_to(args.data_dir).as_posix().encode())
        manifest.update(hashlib.sha256(path.read_bytes()).digest())

    scopes = ["data", "engine"] if args.scope == "both" else [args.scope]
    runs, expected, market_checksums = [], None, set()
    for iteration in range(args.repeats + 1):
        # One excluded warmup per scope/mode; alternate the order to reduce order bias.
        for scope in (scopes if iteration % 2 == 0 else scopes[::-1]):
            for prefetch in ((False, True) if iteration % 2 == 0 else (True, False)):
                run = run_isolated(args, prefetch, scope)
                identity = (run["signal_data_sha256"], run["trading_days"], run["market_rows"],
                            run["prediction_rows"], run["data"]["source_rows"], run["data"]["delivered_rows"],
                            run["data"]["open_rows"], run["data"]["close_rows"])
                if expected is None:
                    expected = identity
                elif identity != expected:
                    raise AssertionError("data/engine, sync/async or repeated playback differs")
                if run["market_checksum"] is not None:
                    market_checksums.add(run["market_checksum"])
                    if len(market_checksums) != 1:
                        raise AssertionError("daily market checksums differ")
                print(f"{'warmup' if iteration == 0 else iteration} {scope} {run['mode']}: "
                      f"{run['elapsed_seconds']:.3f}s, wait={run['data']['wait_seconds']:.3f}s, "
                      f"RSS peak={run['memory']['peak_rss_bytes'] / 2**20:.1f} MiB, "
                      f"mean={run['memory']['mean_rss_bytes'] / 2**20:.1f} MiB", flush=True)
                if iteration:
                    runs.append(run)

    summary = {}
    for scope in scopes:
        summary[scope] = {}
        for mode in ("sync", "async"):
            selected = [run for run in runs if run["scope"] == scope and run["mode"] == mode]
            summary[scope][mode] = {
                key: median(run[key] for run in selected)
                for key in ("elapsed_seconds", "playback_seconds", "inference_seconds",
                            "days_per_second", "source_rows_per_second")
            }
            summary[scope][mode].update({
                key: median(run["data"][key] for run in selected)
                for key in ("calendar_seconds", "read_seconds", "wait_seconds", "as_of_seconds", "market_seconds")
            })
            for metric in ("peak", "mean"):
                summary[scope][mode][f"{metric}_rss_mib"] = median(
                    run["memory"][f"{metric}_rss_bytes"] for run in selected
                ) / 2**20
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": platform.python_version(), "pandas": pd.__version__, "pyarrow": pyarrow.__version__,
            "psutil": psutil.__version__,
            "platform": platform.platform(), "processor": platform.processor(), "logical_cpus": os.cpu_count(),
        },
        "config": {key: str(value) if isinstance(value, Path) else value
                   for key, value in vars(args).items() if key not in ("output", "worker")},
        "dataset": {"files": len(files), "bytes": sum(path.stat().st_size for path in files),
                    "sha256": manifest.hexdigest()},
        "method": "One warmup per scope/mode; alternating order; median of repeats; OS cache not flushed. "
                  "Each run uses a fresh subprocess; startup/import time and parent manifest hashing are excluded. "
                  "Memory covers run_once including engine setup and teardown: total process RSS (Windows working set), "
                  "including the loaded runtime, native allocations and all threads, sampled at memory_sample_ms. "
                  "Peak is the sampled maximum; mean is the time-weighted trapezoidal estimate. "
                  "Sampling may miss short peaks; actual gaps are reported. Sampling overhead is included. "
                  "Framework DEBUG logs disabled; worker JSON output and per-run console summaries occur after measurement. "
                  "Both scopes include signal checks/hashes and mock inference; data scope also touches daily prices. "
                  "Engine scope includes runtime cache exchanges, daily snapshots and financial placeholders; "
                  "data scope calls no financial modules. "
                  "async_inference controls a separate single model worker for the engine scope; "
                  "the sync/async mode labels still refer only to data reading. "
                  "Sleep models only overlap opportunity.",
        "identical_playback": True, "summary": summary, "runs": runs,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
