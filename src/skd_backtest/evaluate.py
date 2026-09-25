"""Python API（个人单模型回测）：
用户对自己的单个模型进行回测时，建议参考 examples/basic_usage.py 使用 Python API。
将已初始化模型的 model.predict 作为 inference 传给 BacktestEngine。

命令行评测（评测平台批量回测）：
skd-backtest-evaluate 是本包提供的命令行评测入口，推荐用于评测平台批量回测时使用。
每次调用处理一个标准模型提交，由平台调度多个调用。"""
import argparse
import json
from pathlib import Path
import tomllib

from .config import CostConfig, DataCapabilities, FeeScheduleEntry, OptimizerConfig, ReferenceSources
from .engine import BacktestEngine


# Keep the displayed example identical to examples/basic_usage.py.
_BASIC_USAGE_EXAMPLE = r'''
"""Replay 2016-2022 with a naive equal-score, equal-weight CSI300 portfolio."""

from pprint import pprint
import logging

import pandas as pd

from skd_backtest import BacktestEngine, OptimizerConfig


class InferenceModel:
    def __init__(self, model_dir: str):
        # naive 模型没有训练参数，保留标准 model_dir 接口。
        logging.getLogger(__name__).debug("[UserModel.__init__] model_dir=%s", model_dir)

    def predict(self, as_of_date, data):
        # naive 模型给当日全部沪深300成分股相同的零分。
        day = int(as_of_date.replace("-", ""))
        codes = data["Barra_factor"].loc[lambda table: table["日期"] == day, "代码"]
        return pd.DataFrame({"date": as_of_date, "code": codes, "score": 0.0})


if __name__ == "__main__":
    model = InferenceModel("./submission/model/")
    engine = BacktestEngine(
        data_dir=r"D:\Data",
        start_date="2016-01-01",
        end_date="2022-12-31",
        inference=model.predict,
        optimizer_config=OptimizerConfig(top_k=300),  # 全部成分股的目标权重均为 1/300。
    )
    metrics = engine.run()
    if not engine.config.friendly_output:
        pprint(metrics)
        pprint(engine.performance)
'''


def load_config(path):
    path = Path(path).resolve()
    if path.suffix.lower() == ".json":
        document = json.loads(path.read_text(encoding="utf-8"))
    elif path.suffix.lower() == ".toml":
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    else:
        raise ValueError("evaluation config must be JSON or TOML")
    allowed = {"backtest", "optimizer", "costs", "data_capabilities", "reference_sources"}
    if not isinstance(document, dict) or set(document) - allowed:
        raise ValueError("unknown evaluation configuration section")
    options = dict(document["backtest"])
    # Relative data paths belong to the configuration, not to the participant.
    for name in ("data_dir", "output_dir"):
        if options.get(name) is not None:
            options[name] = path.parent / options[name]
    if "inference" in options or "submission_dir" in options:
        raise ValueError("submission is selected only by --submission")
    optimizer = dict(document.get("optimizer", {}))
    if "barra_factors" in optimizer:
        optimizer["barra_factors"] = tuple(optimizer["barra_factors"])
    costs = dict(document.get("costs", {}))
    costs["fee_schedule"] = tuple(FeeScheduleEntry(**entry) for entry in costs.get("fee_schedule", []))
    sources = {name: path.parent / value for name, value in document.get("reference_sources", {}).items()
               if value is not None}
    options.update(
        optimizer_config=OptimizerConfig(**optimizer),
        cost_config=CostConfig(**costs),
        data_capabilities=DataCapabilities(**document.get("data_capabilities", {})),
        reference_sources=ReferenceSources(**sources),
    )
    return options


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="skd-backtest-evaluate",
        description=__doc__,
        epilog="单模型回测示例（examples/basic_usage.py；请按实际情况调整 data_dir 和模型）：\n"
               + _BASIC_USAGE_EXAMPLE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument("-h", "-help", "--help", action="help",
                        help="显示使用说明和完整单模型回测示例后退出")
    parser.add_argument("--submission", type=Path, required=True, help="含 inference.py 和 model/ 的标准提交目录")
    parser.add_argument("--config", type=Path, required=True, help="统一评测使用的 JSON 或 TOML 配置")
    parser.add_argument("--friendly-output", action=argparse.BooleanOptionalAction, default=None,
                        help="show progress and a result table (default: enabled; overrides config)")
    args = parser.parse_args(argv)
    options = load_config(args.config)
    if args.friendly_output is not None:
        options["friendly_output"] = args.friendly_output
    # The evaluation entry always emits the required audit files.
    if options.get("output_dir") is None:
        options["output_dir"] = Path("result") / args.submission.resolve().name
    engine = BacktestEngine(submission_dir=args.submission, **options)
    metrics = engine.run()
    if not engine.config.friendly_output:
        print(json.dumps(metrics, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
