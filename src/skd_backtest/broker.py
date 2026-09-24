"""A-share execution boundary; every trading rule is still a placeholder."""

import logging

import pandas as pd

from .cost_model import CostModel
from .schemas import empty_result


logger = logging.getLogger(__name__)


class Broker:
    def initialize(self, initial_cash: float) -> dict:
        logger.debug("[Broker.initialize] initial account; no positions")
        return {
            "cash": initial_cash,
            "total_shares": {},
            "sellable_shares": {},
            "market_value": 0.0,
            "portfolio_value": initial_cash,
        }

    def start_day(self, *, date: str, account: dict) -> None:
        # TODO: 每个交易日日初释放昨日买入股数为 sellable_shares，落实 T+1。
        # 无调仓日也必须处理，不依赖 execute 是否被调用。
        logger.debug("[Broker.start_day] STUB date=%s; settle T+1", date)

    def execute(self, *, date: str, target_weights: pd.DataFrame,
                market: pd.DataFrame, account: dict, cost_model: CostModel,
                price_mode: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        logger.debug("[Broker.execute] STUB date=%s, mode=%s; no orders executed", date, price_mode)
        # TODO: 开盘检查停牌/有效价格/涨跌停；读取 PIT 限价，不能硬编码 +/-10%。
        logger.debug("[Broker.tradability] STUB T+1 / suspension / price limits")
        # TODO: raw_price: equity * target_weight / raw_open -> 股数差 -> 整手。
        # 买入按 100 股整手，卖出允许剩余零股清仓，不超过可卖数量。
        # adjusted_return: 后续独立实现权重收益路径，不以复权价做真实股数撮合。
        logger.debug("[Broker.target_shares] STUB target portfolio -> requested orders")
        # TODO: 先卖出，按成功成交及费用更新真实现金，再缩减买单，禁止负现金。
        # 失败卖单保留持仓；记录 LIMIT_UP/LIMIT_DOWN/SUSPENDED/
        # INSUFFICIENT_CASH/T1_NOT_SELLABLE，不把目标仓位当作实际仓位。
        logger.debug("[Broker.sell_orders] STUB sell -> update cash")
        cost_model.calculate(date=date, side=None, trade_value=None)
        logger.debug("[Broker.buy_orders] STUB buy -> update cash and positions")
        return empty_result("orders"), empty_result("trades")
