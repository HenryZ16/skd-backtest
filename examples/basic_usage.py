"""Run after installing skd-backtest; no real data is read at this stage."""

from pprint import pprint

import pandas as pd

from skd_backtest import BacktestEngine, OptimizerConfig


class InferenceModel:
    def __init__(self, model_dir: str):
        # 实际模型在此加载一次已训练参数；本例不加载文件。
        print(f"[UserModel.__init__] model_dir={model_dir}")

    def predict(self, as_of_date, data):
        print(f"[UserModel.predict] {as_of_date}; shapes="
              f"{ {name: table.shape for name, table in data.items()} }")
        # 后续在此执行特征处理和预测；本轮空表演示只返回固定输出列。
        return pd.DataFrame(columns=["date", "code", "score"])


if __name__ == "__main__":
    model = InferenceModel("./submission/model/")
    engine = BacktestEngine(
        data_dir=r"D:\Data",
        start_date="2024-01-02",
        end_date="2024-12-31",
        inference=model.predict,
        optimizer_config=OptimizerConfig(top_k=50),
    )
    pprint(engine.run())
