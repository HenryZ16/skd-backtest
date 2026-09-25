"""Evaluate a standard submission using a shared JSON or TOML configuration."""
import argparse
import json
from pathlib import Path
import tomllib

from .config import CostConfig, DataCapabilities, FeeScheduleEntry, OptimizerConfig, ReferenceSources
from .engine import BacktestEngine


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    options = load_config(args.config)
    # The evaluation entry always emits the required audit files.
    if options.get("output_dir") is None:
        options["output_dir"] = Path("result") / args.submission.resolve().name
    engine = BacktestEngine(submission_dir=args.submission, **options)
    metrics = engine.run()
    print(json.dumps(metrics, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
