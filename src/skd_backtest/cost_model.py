"""Fee-quotation placeholder; historical fees and slippage remain unimplemented."""

from .config import CostConfig
from .contracts import CostQuote, Topic
from .runtime_cache import CacheView


class CostModel:
    def __init__(self, config: CostConfig):
        self.config = config

    def calculate(self, *, date: str, request_id: int, cache: CacheView) -> None:
        request = cache.read(Topic.COST_REQUEST, (date, request_id))
        # TODO: Apply directional slippage and the transaction date's commission/tax schedule.
        value = (request.base_price * request.shares if request.price_mode == "raw_price"
                 else request.position_value)
        cache.log(level="DEBUG", message="STUB fee quote: zero fees and no slippage")
        cache.publish(Topic.COST_RESULT, (date, request_id), CostQuote(
            request_id, request.order_id, date, request.side, request.price_mode,
            request.base_price, value, value, 0.0, 0.0, 0.0, 0.0,
            -value if request.side == "BUY" else value,
        ))
