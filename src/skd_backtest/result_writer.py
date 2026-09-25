"""Run-log lifecycle and final output boundary; JSON/CSV export is not implemented."""

from dataclasses import asdict
import json
from pathlib import Path

from .contracts import OutputReceipt, Topic
from .runtime_cache import CacheView
from .schemas import RESULT_COLUMNS


class ResultWriter:
    def __init__(self, output_dir: Path | None):
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self._log = None
        self._cursor = 0

    def open(self, *, cache: CacheView) -> None:
        if self._log is not None:
            raise RuntimeError("writer is already open")
        self._cursor = 0
        if self.output_dir is not None:
            names = ["metrics.json", "run.log", *(name + ".csv" for name in RESULT_COLUMNS)]
            if any((self.output_dir / name).exists() for name in names):
                raise FileExistsError("output directory already contains run results")
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._log = (self.output_dir / "run.log").open("x", encoding="utf-8", newline="\n")
        context = cache.read(Topic.RUN_CONTEXT)
        cache.log(level="INFO", message="run configuration",
                  details=json.loads(json.dumps(asdict(context), default=str, allow_nan=False)))
        self.flush_log(cache=cache)

    def flush_log(self, *, cache: CacheView) -> None:
        for record in cache.log_records(after_seq=self._cursor):
            if self._log is not None:
                self._log.write(json.dumps(asdict(record), ensure_ascii=False, allow_nan=False) + "\n")
                self._log.flush()
            self._cursor = record.seq

    def write(self, *, cache: CacheView) -> None:
        # TODO: Serialize metrics and the seven CSVs, then publish a written receipt.
        cache.log(level="DEBUG", message="STUB result export: financial JSON/CSV files not written")
        cache.publish(Topic.OUTPUT_RECEIPT, None, OutputReceipt("disabled", None, {}))

    def close(self, *, cache: CacheView) -> None:
        try:
            self.flush_log(cache=cache)
        finally:
            if self._log is not None:
                self._log.close()
                self._log = None
