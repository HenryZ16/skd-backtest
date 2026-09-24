"""Future audit-file output boundary; currently logs the intended files at DEBUG."""

import logging

from pathlib import Path

import pandas as pd

from .schemas import RESULT_COLUMNS


logger = logging.getLogger(__name__)


class ResultWriter:
    def __init__(self, output_dir: Path | None):
        self.output_dir = output_dir

    def write(self, *, metrics: dict[str, float | int | None], tables: dict[str, pd.DataFrame]) -> None:
        # TODO: output_dir 配置后写 metrics.json、七张审计 CSV 和 run.log。
        # 字段由 RESULT_COLUMNS 定义，保持 score -> target -> order -> trade -> NAV 审计链。
        # JSON 中 None 写为 null；没有输出目录时仅返回内存结果。当前完全不写文件。
        logger.debug("[ResultWriter.write] STUB output_dir=%s; no files written", self.output_dir)
        if logger.isEnabledFor(logging.DEBUG):
            files = ["metrics.json", *(f"{name}.csv" for name in RESULT_COLUMNS), "run.log"]
            logger.debug("[ResultWriter.files] %s", ", ".join(files))
