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
