"""Call a user-owned function or an already initialized model's bound method."""

from collections.abc import Callable

import pandas as pd

Inference = Callable[..., pd.DataFrame]


class SubmissionRunner:
    def __init__(self, inference: Inference):
        self.inference = inference

    def predict(self, as_of_date: str, data: dict[str, pd.DataFrame]) -> pd.DataFrame:
        # 用户在引擎外加载一次模型，然后传入 model.predict；不动态导入或重新训练。
        # 约定返回 date/code/score，覆盖当日成分且分数有限；信任输入，不重复验证。
        print(f"[SubmissionRunner.predict] calling user inference at {as_of_date}")
        return self.inference(as_of_date=as_of_date, data=data)
