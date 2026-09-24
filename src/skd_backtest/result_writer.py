"""Future audit-file output boundary; currently prints the intended files."""

from pathlib import Path

import pandas as pd

from .schemas import RESULT_COLUMNS


class ResultWriter:
    def __init__(self, output_dir: Path | None):
        self.output_dir = output_dir

    def write(self, *, metrics: dict[str, float | int | None], tables: dict[str, pd.DataFrame]) -> None:
        # TODO: output_dir 配置后写 metrics.json、七张审计 CSV 和 run.log。
        # 字段由 RESULT_COLUMNS 定义，保持 score -> target -> order -> trade -> NAV 审计链。
        # JSON 中 None 写为 null；没有输出目录时仅返回内存结果。当前完全不写文件。
        files = ["metrics.json", *(f"{name}.csv" for name in RESULT_COLUMNS), "run.log"]
        print(f"[ResultWriter.write] STUB output_dir={self.output_dir}; no files written")
        print(f"[ResultWriter.files] {', '.join(files)}")
