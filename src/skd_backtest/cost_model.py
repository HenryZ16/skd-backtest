"""Date-dependent fees and slippage."""

from .config import CostConfig


class CostModel:
    def __init__(self, config: CostConfig):
        self.config = config

    def calculate(self, *, date: str | None, side: str | None,
                  trade_value: float | None) -> dict[str, float | None]:
        # TODO: 按 date 选择生效费率，计算佣金/最低佣金、卖出印花税、过户费。
        # Broker 按买卖方向将 slippage 应用于开盘价；不得全历史写死同一税率。
        # 当前 None 参数只用于空流程演示，不代表发生了金额为零的交易。
        print(f"[CostModel.calculate] STUB date={date}; no fees calculated")
        return dict.fromkeys(("commission", "stamp_tax", "other_cost", "total_cost"))
