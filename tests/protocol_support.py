"""Synthetic component publications for orchestration tests, never investment results."""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pandas as pd

from skd_backtest.contracts import (
    CloseSnapshot, CostQuote, CostRequest, ExecutionResult, OpenSnapshot,
    PredictionResult, TargetPlan, Topic,
)
from skd_backtest.schemas import (
    LABEL_COLUMNS, METRIC_NAMES, RESULT_COLUMNS, VALUE_COLUMNS, WEIGHT_COLUMNS, empty_result,
)


@contextmanager
def protocol_components(engine, trace=None):
    trace = trace if trace is not None else []

    def settle(*, date, cache):
        trace.append(("settle", date))
        dates = cache.read(Topic.RUN_CALENDAR).trading_dates
        index = dates.index(date)
        state = (cache.read(Topic.ACCOUNT_CLOSE, dates[index - 1]).account if index
                 else cache.read(Topic.ACCOUNT_INITIAL).account)
        cache.publish(Topic.ACCOUNT_SETTLED, date, state)

    def open_mark(*, date, market, cache):
        trace.append(("open", date))
        assert "adjusted_close" not in market
        state = cache.read(Topic.ACCOUNT_SETTLED, date)
        cache.publish(Topic.ACCOUNT_OPEN, date,
                      OpenSnapshot(date, state, pd.DataFrame(columns=VALUE_COLUMNS), 0.0, state.cash))

    def execute(*, date, market, cache):
        trace.append(("execute", date))
        opening = cache.read(Topic.ACCOUNT_OPEN, date)
        assert "adjusted_close" not in market
        signal = None
        try:
            if cache.contains(Topic.SIGNAL_TARGETS, date):
                plan = cache.read(Topic.SIGNAL_TARGETS, date)
                signal = plan.signal_date
                trace.append(("target", signal, date))
                # A deliberately discarded quote verifies orchestration without creating a fill.
                cache.publish(Topic.COST_REQUEST, (date, 1),
                              CostRequest(1, date + "-quote", date, "BUY", "adjusted_return", None, None, 10.0))
                yield 1
                assert cache.read(Topic.COST_RESULT, (date, 1)).trade_value == 10.0
                cache.finish_quote(date=date, request_id=1)
            cache.publish(Topic.EXECUTION_DAY, date,
                          ExecutionResult(date, opening.account, empty_result("orders"), empty_result("trades"),
                                          0.0, 0.0, signal))
        finally:
            trace.append(("execution_closed", date))

    def cost(*, date, request_id, cache):
        trace.append(("cost", date, request_id))
        request = cache.read(Topic.COST_REQUEST, (date, request_id))
        cache.publish(Topic.COST_RESULT, (date, request_id),
                      CostQuote(request_id, request.order_id, date, request.side, request.price_mode,
                                None, 10.0, 10.0, 0.0, 0.0, 0.0, 0.0, -10.0))

    def close_mark(*, date, market, cache):
        trace.append(("close", date))
        assert "adjusted_open" not in market
        state = cache.read(Topic.EXECUTION_DAY, date).account
        assert cache.read(Topic.ACCOUNT_OPEN, date).portfolio_value == state.cash
        equity = pd.DataFrame([dict(date=date, cash=state.cash, market_value=0.0, portfolio_value=state.cash,
                                   portfolio_nav=1.0, portfolio_return=0.0, turnover=0.0, transaction_cost=0.0)],
                              columns=RESULT_COLUMNS["equity_curve"])
        cache.publish(Topic.ACCOUNT_CLOSE, date,
                      CloseSnapshot(date, state, empty_result("positions"), equity, pd.DataFrame(columns=WEIGHT_COLUMNS)))

    def optimize(*, signal_date, cache):
        trace.append(("optimize", signal_date))
        assert cache.read(Topic.ACCOUNT_CLOSE, signal_date).equity_curve.date.tolist() == [signal_date]
        assert cache.read(Topic.SIGNAL_SCORES, signal_date).date.eq(signal_date).all()
        calendar = cache.read(Topic.RUN_CALENDAR).signal_calendar.set_index("signal_date")
        execution = calendar.at[signal_date, "execution_date"]
        cache.publish(Topic.SIGNAL_TARGETS, execution,
                      TargetPlan(signal_date, execution, empty_result("target_weights")))

    def labels(*, cache):
        trace.append(("labels",))
        cache.publish(Topic.EVALUATION_LABELS, None, pd.DataFrame(columns=LABEL_COLUMNS))

    def evaluate(*, cache):
        trace.append(("evaluate",))
        cache.read(Topic.EVALUATION_LABELS)
        scores = cache.history(Topic.SIGNAL_SCORES)
        cache.publish(Topic.EVALUATION_PREDICTION, None,
                      PredictionResult(scores.assign(future_return=None), empty_result("rankic")))

    def metrics(*, cache):
        trace.append(("metrics",))
        cache.result_tables()
        cache.publish(Topic.EVALUATION_METRICS, None, dict.fromkeys(METRIC_NAMES))

    with ExitStack() as stack:
        for component, method, function in (
            (engine.broker, "start_day", settle),
            (engine.accounting, "mark_at_open", open_mark), (engine.broker, "execute", execute),
            (engine.cost_model, "calculate", cost), (engine.accounting, "mark_to_market", close_mark),
            (engine.optimizer, "optimize", optimize), (engine.label_provider, "build", labels),
            (engine.prediction_evaluator, "evaluate", evaluate), (engine.metrics_calculator, "calculate", metrics),
        ):
            stack.enter_context(patch.object(component, method, new=function))
        yield trace
