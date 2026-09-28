"""Run Barra exposure constraints directly with the existing D:/Data datasets."""

from pathlib import Path

import pandas as pd

from skd_backtest import BacktestEngine, OptimizerConfig


def predict(*, as_of_date: str, data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    # Replace this demonstration score with your model; score every current member.
    day = int(as_of_date.replace("-", ""))
    today = data["Barra_factor"].loc[lambda frame: frame["日期"].eq(day)]
    return pd.DataFrame(
        {"date": as_of_date, "code": today["代码"], "score": today["动量"]}
    )


if __name__ == "__main__":
    data_dir = Path(r"D:\Data")
    engine = BacktestEngine(
        data_dir=data_dir,
        start_date="2022-01-01",
        end_date="2022-12-31",
        inference=predict,
        lookback=1,  # This demo reads only the current factor cross-section.
        output_dir="result/barra",
        optimizer_config=OptimizerConfig(
            method="barra",
            single_name_weight_limit=0.10,
            active_weight_limit=0.01,
            industry_exposure_limit=0.02,
            barra_style_exposure_limit=0.02,  # Units of the supplied 0-1 style percentiles.
            turnover_limit=1.0,  # Two-sided turnover; initial investment needs at least 1.
        ),
    )
    engine.run()
