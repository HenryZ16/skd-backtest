"""Synthetic raw-open input shared by market playback fixtures."""

from pathlib import Path
import pandas as pd


def write_raw_open(root):
    root = Path(root)
    for source in (root / "MarketData").glob("*/*/*.parquet"):
        raw = pd.read_parquet(source, columns=["日期", "代码", "open"])
        raw["open"] = raw["open"] / 2.0
        target = root / "MarketDataRawOpen" / source.relative_to(root / "MarketData")
        target.parent.mkdir(parents=True, exist_ok=True)
        raw.to_parquet(target, index=False)
