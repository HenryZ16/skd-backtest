"""Diagnostic worker. Ablations are process-local and must not be used as backtests."""

import argparse
from collections import Counter
from contextlib import ExitStack, nullcontext
import cProfile
import hashlib
import json
import os
from pathlib import Path
import platform
import pstats
import subprocess
import sys
import tempfile
from threading import Event, Thread, get_ident
from time import perf_counter, process_time, sleep, thread_time
from unittest.mock import patch

import pandas as pd
import psutil
import pyarrow

ROOT = Path(__file__).resolve().parents[2]


class StackSampler:
    """Statistical main-thread stacks; inclusive counts overlap, not exact timings."""

    def __init__(self):
        self.owner = get_ident()
        self.stop = Event()
        self.samples = 0
        self.leaves, self.inclusive = Counter(), Counter()
        self.gaps = []
        self.thread = Thread(target=self.poll, name="profile-stack")

    def poll(self):
        previous = perf_counter()
        while not self.stop.wait(0.005):
            now = perf_counter()
            self.gaps.append(now - previous)
            previous = now
            frame = sys._current_frames().get(self.owner)
            frames = []
            while frame:
                frames.append((frame.f_code.co_filename, frame.f_code.co_firstlineno, frame.f_code.co_name))
                frame = frame.f_back
            if frames:
                self.samples += 1
                self.leaves[frames[0]] += 1
                self.inclusive.update(set(frames))

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()

    def report(self):
        def rows(counter):
            return [dict(file=key[0], line=key[1], function=key[2], samples=count,
                         fraction=count / self.samples) for key, count in counter.most_common(80)]
        return dict(samples=self.samples, mean_gap_seconds=sum(self.gaps) / len(self.gaps),
                    max_gap_seconds=max(self.gaps), by_leaf=rows(self.leaves), by_inclusive=rows(self.inclusive))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("current", "head"), default="current")
    parser.add_argument("--ref", default="HEAD", help="Git revision for --source head")
    parser.add_argument("--mode", choices=("baseline", "cprofile", "inventory", "no_object_map",
                                         "no_clone", "no_frame_check", "share_calendar", "sample", "empty_frame_fastpath"),
                        default="baseline")
    parser.add_argument("--end", default="2022-12-31")
    parser.add_argument("--sync", action="store_true", help="disable background data reading")
    parser.add_argument("--async-inference", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--inference-delay-ms", type=float, default=0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.inference_delay_ms < float("inf"):
        parser.error("inference delay must be finite and nonnegative")
    if args.source == "current" and args.mode == "cprofile" and args.async_inference:
        parser.error("cprofile measures the calling thread; use --no-async-inference")
    if args.source == "head" and args.mode not in ("baseline", "cprofile"):
        parser.error("the pre-cache comparison supports baseline/cprofile only")

    with ExitStack() as stack:
        if args.source == "head":
            temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="skd-profile-head-"))
            target = Path(temporary).resolve()
            assert target.parent == Path(tempfile.gettempdir()).resolve()
            assert target.name.startswith("skd-profile-head-")
            names = subprocess.check_output(
                ["git", "ls-tree", "-r", "--name-only", args.ref, "src/skd_backtest"],
                cwd=ROOT, text=True).splitlines()
            for name in names:
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(subprocess.check_output(["git", "show", args.ref + ":" + name], cwd=ROOT))
            sys.path.insert(0, str(target / "src"))
        else:
            sys.path.insert(0, str(ROOT / "src"))
        sys.path.insert(1, str(ROOT))

        from skd_backtest import BacktestEngine, OptimizerConfig
        from examples.benchmark_data_flow import MemorySampler
        from examples.basic_usage import InferenceModel

        model = InferenceModel("./submission/model/")
        def inference(**kwargs):
            if args.inference_delay_ms:
                sleep(args.inference_delay_ms / 1000)
            return model.predict(**kwargs)

        extra = {"async_inference": args.async_inference, "friendly_output": False} if args.source == "current" else {}
        engine = BacktestEngine(data_dir=r"D:\Data", start_date="2016-01-01",
                                end_date=args.end, inference=inference,
                                optimizer_config=OptimizerConfig(top_k=50), prefetch=not args.sync, **extra)
        counts = Counter()
        groups = Counter()
        if args.source == "current":
            import skd_backtest.runtime_cache as cache
            from skd_backtest.contracts import RunCalendar
            original_clone = getattr(cache, "_clone", None)
            original_frame = cache._frame
            if original_clone is None and args.mode in (
                    "empty_frame_fastpath", "no_object_map", "no_clone", "share_calendar"):
                parser.error("this ablation requires the pre-optimization cache; use baseline/cprofile/inventory")
            if args.mode == "empty_frame_fastpath":
                def clone(value):
                    return value.copy(deep=True) if isinstance(value, pd.DataFrame) and value.empty else original_clone(value)
                stack.enter_context(patch.object(cache, "_clone", clone))
            elif args.mode == "no_object_map":
                def clone(value):
                    return value.copy(deep=True) if isinstance(value, pd.DataFrame) else original_clone(value)
                stack.enter_context(patch.object(cache, "_clone", clone))
            elif args.mode == "no_clone":
                stack.enter_context(patch.object(cache, "_clone", lambda value: value))
            elif args.mode == "no_frame_check":
                stack.enter_context(patch.object(cache, "_frame", lambda *a, **kw: None))
            elif args.mode == "share_calendar":
                def clone(value):
                    return value if isinstance(value, RunCalendar) else original_clone(value)
                stack.enter_context(patch.object(cache, "_clone", clone))
            elif args.mode == "inventory":
                def clone(value):
                    counts["clone_calls"] += 1
                    if isinstance(value, pd.DataFrame):
                        counts["frame_clones"] += 1
                        counts["empty_frame_clones"] += int(value.empty)
                        counts["frame_rows"] += len(value)
                        counts["frame_cells"] += value.size
                        counts["frame_array_payload_bytes"] += sum(block.values.nbytes for block in value._mgr.blocks)
                        groups["clone:" + ",".join(map(str, value.columns))] += 1
                    return original_clone(value)
                def frame(value, *a, **kw):
                    counts["frame_checks"] += 1
                    counts["empty_frame_checks"] += int(isinstance(value, pd.DataFrame) and value.empty)
                    return original_frame(value, *a, **kw)
                if original_clone is not None:
                    stack.enter_context(patch.object(cache, "_clone", clone))
                stack.enter_context(patch.object(cache, "_frame", frame))

        profiler = cProfile.Profile() if args.mode == "cprofile" else None
        process = psutil.Process()
        io_before = process.io_counters()
        monitor = nullcontext(None) if profiler else MemorySampler(0.05)
        sampler = StackSampler() if args.mode == "sample" else None
        with monitor as memory, (sampler if sampler else nullcontext()):
            wall_start, cpu_start, thread_start = perf_counter(), process_time(), thread_time()
            if profiler:
                profiler.enable()
            engine.run()
            if profiler:
                profiler.disable()
            wall, cpu, main_cpu = perf_counter() - wall_start, process_time() - cpu_start, thread_time() - thread_start
        io_after = process.io_counters()
        # Output comparison occurs outside measurement and uses canonical row ordering.
        outputs = {}
        for name, table in engine.tables.items():
            ordered = table
            if "date" in table and "code" in table:
                ordered = table.sort_values(["date", "code"], ignore_index=True)
            outputs[name] = dict(rows=len(table), columns=list(table.columns),
                                 sha256=hashlib.sha256(ordered.to_json(orient="split", index=False).encode()).hexdigest())
        result = dict(source=args.source, source_ref=args.ref if args.source == "head" else None,
                      mode=args.mode, end=args.end, prefetch=not args.sync,
                      async_inference=args.async_inference if args.source == "current" else False,
                      inference_delay_ms=args.inference_delay_ms,
                      wall_seconds=wall, process_cpu_seconds=cpu, main_thread_cpu_seconds=main_cpu,
                      process_cpu_over_wall=cpu / wall, main_thread_cpu_over_wall=main_cpu / wall,
                      rss_peak_mib=memory.stats["peak_rss_bytes"] / 2**20 if memory else None,
                      rss_mean_mib=memory.stats["mean_rss_bytes"] / 2**20 if memory else None,
                      io_read_bytes=io_after.read_bytes - io_before.read_bytes,
                      io_write_bytes=io_after.write_bytes - io_before.write_bytes,
                      performance=engine.performance, outputs=outputs, metrics=engine.metrics,
                      inventory=dict(counts), clone_schemas=dict(groups),
                      environment=dict(python=platform.python_version(), pandas=pd.__version__,
                                       pyarrow=pyarrow.__version__, psutil=psutil.__version__,
                                       processor=os.environ.get("PROCESSOR_IDENTIFIER"),
                                       logical_cpus=psutil.cpu_count(), platform=platform.platform()),
                      head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip())
        if args.source == "current":
            result["source_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in sorted((ROOT / "src/skd_backtest").glob("*.py"))}
        if sampler:
            result["sampling"] = sampler.report()
        if profiler:
            stats = pstats.Stats(profiler)
            rows = []
            for (file, line, function), (primitive, calls, own, cumulative, callers) in stats.stats.items():
                rows.append(dict(file=file, line=line, function=function, primitive_calls=primitive,
                                 calls=calls, self_seconds=own, cumulative_seconds=cumulative))
            result["profile"] = dict(total_calls=stats.total_calls, primitive_calls=stats.prim_calls,
                                     total_seconds=stats.total_tt,
                                     by_cumulative=sorted(rows, key=lambda r:r["cumulative_seconds"], reverse=True)[:100],
                                     by_self=sorted(rows, key=lambda r:r["self_seconds"], reverse=True)[:100],
                                     project=[r for r in rows if "skd_backtest" in r["file"]],
                                     copies=[r for r in rows if "copy" in r["function"]])
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key:result[key] for key in ("source", "mode", "end", "wall_seconds",
                         "process_cpu_seconds", "main_thread_cpu_seconds", "rss_peak_mib")}), flush=True)


if __name__ == "__main__":
    main()
