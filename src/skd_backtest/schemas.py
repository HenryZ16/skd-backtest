"""Empty table contracts; source column names match D:\\Data."""

import pandas as pd

SOURCE_COLUMNS = {
    "Factor33_winsor": (
        "日期", "代码", "名称", "turn", "change_ratio", "daily_return",
        "momentum_5", "reversal_5", "volatility_5", "sma_20", "ema_20",
        "macd_diff_12_26_9", "macd_dea_12_26_9", "macd_hist_12_26_9",
        "rsi_12", "kdj_k_9_3_3", "kdj_d_9_3_3", "bias_20", "cci_14", "atr_14",
        "total_market_cap", "float_market_cap", "pe_ttm", "pb", "ps_ttm",
        "roe_avg_ttm", "roa_avg_ttm", "gross_profit_rate_ttm",
        "net_profit_rate_ttm", "debt_to_asset_lf", "current_ratio_lf",
        "beta_000300SH_22", "list_days", "is_suspend", "is_st", "is_new",
    ),
    "Barra_factor": (
        "日期", "代码", "名称", "市值", "贝塔", "动量", "残差波动", "非线性市值",
        "账面市值比", "流动性", "盈利", "成长", "杠杆",
    ),
    "MarketData": (
        "日期", "代码", "名称", "open", "high", "low", "close",
        "volume", "amount", "is_suspend",
    ),
}

RESULT_COLUMNS = {
    "predictions": ("date", "code", "score", "future_return"),
    "rankic": ("date", "rankic", "n_stocks"),
    "target_weights": (
        "signal_date", "execution_date", "code", "score",
        "benchmark_weight", "target_weight",
    ),
    "orders": ("date", "code", "side", "requested_shares", "status", "reject_reason"),
    "trades": (
        "date", "code", "side", "shares", "price", "trade_value",
        "commission", "stamp_tax", "other_cost", "total_cost",
    ),
    "positions": ("date", "code", "shares", "sellable_shares", "close", "market_value", "weight"),
    "equity_curve": (
        "date", "cash", "market_value", "portfolio_value", "portfolio_nav",
        "portfolio_return", "benchmark_nav", "benchmark_return", "active_return",
        "turnover", "transaction_cost",
    ),
}


def empty_result(name: str) -> pd.DataFrame:
    return pd.DataFrame(columns=RESULT_COLUMNS[name])
