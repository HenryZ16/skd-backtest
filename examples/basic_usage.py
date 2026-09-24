"""Replay 2016-2022 daily data with a user-owned inference callback."""

from pprint import pprint
import logging

import pandas as pd

from skd_backtest import BacktestEngine, OptimizerConfig


class InferenceModel:
    def __init__(self, model_dir: str):
        # 实际模型在此加载一次已训练参数；本例不加载文件。
        logging.getLogger(__name__).debug("[UserModel.__init__] model_dir=%s", model_dir)

    def predict(self, as_of_date, data):
        # 演示按当日合法成分输出零分；模型可以使用整个可见历史窗口。
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
        optimizer_config=OptimizerConfig(top_k=50),
    )
    pprint(engine.run())
    pprint(engine.performance)
