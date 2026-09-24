"""Prediction evaluation never exposes labels to inference."""

import pandas as pd

from .schemas import empty_result


class PredictionEvaluator:
    def evaluate(self, *, scores: pd.DataFrame, future_returns: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        # TODO: 按 date/code 对齐分数和完整未来收益，逐调仓日计算 Spearman RankIC。
        # 预测能力单独评价，不受订单成交结果影响；不向推理和优化器反馈标签。
        print("[PredictionEvaluator.evaluate] STUB future-return labels -> RankIC")
        predictions = scores.loc[:, ["date", "code", "score"]].copy()
        predictions["future_return"] = None
        return predictions, empty_result("rankic")
