"""Console presentation of playback progress and already-calculated metrics."""

from pathlib import Path
import sys
from time import perf_counter
from unicodedata import east_asian_width


_METRIC_DISPLAY = (
    ("mean_rankic", "平均 RankIC", ".4f"),
    ("rankic_std", "RankIC 标准差", ".4f"),
    ("rankic_ir", "RankICIR（不年化）", ".4f"),
    ("positive_rankic_ratio", "RankIC 正值比例", ".2%"),
    ("total_return", "累计收益率", ".2%"),
    ("annualized_return", "年化收益率", ".2%"),
    ("annualized_excess_return", "年化超额收益率", ".2%"),
    ("annualized_volatility", "年化波动率", ".2%"),
    ("maximum_drawdown", "最大回撤", ".2%"),
    ("tracking_error", "跟踪误差", ".2%"),
    ("information_ratio", "信息比率", ".4f"),
    ("sharpe_ratio", "夏普比率", ".4f"),
    ("turnover", "累计换手率", ".2%"),
    ("transaction_cost", "交易费用合计", ",.2f"),
    ("failed_orders", "失败订单数", ",d"),
)


def _width(text):
    return sum(2 if east_asian_width(char) in "WF" else 1 for char in text)


class ConsoleReporter:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.stream = sys.stderr
        self.terminal = enabled and self.stream.isatty()
        self.line_width = 0
        self.started = perf_counter()
        self.last_update = float("-inf")

    def close(self):
        if self.line_width:
            print(file=self.stream, flush=True)
            self.line_width = 0

    def status(self, message):
        if self.enabled:
            self.close()
            print(message, file=self.stream, flush=True)

    def progress(self, completed, total, date=None):
        if not self.enabled:
            return
        if total == 0:
            self.status("回放：区间内无交易日。")
            return
        now = perf_counter()
        if completed == 0:
            self.started = now
        # Refresh a terminal at most five times per second; keep redirected logs sparse.
        interval = 0.2 if self.terminal else 5.0
        if completed not in (0, 1, total) and now - self.last_update < interval:
            return
        self.last_update = now
        elapsed = now - self.started
        message = f"回放 {completed / total:6.1%} | {completed}/{total} 交易日"
        if date is not None:
            message += f" | {date}"
        message += f" | 已用 {elapsed:.1f}s"
        if 0 < completed < total:
            message += f" | 剩余约 {elapsed / completed * (total - completed):.1f}s"
        if self.terminal:
            width = _width(message)
            print("\r" + message + " " * max(0, self.line_width - width),
                  end="", file=self.stream, flush=True)
            self.line_width = width
        else:
            print(message, file=self.stream, flush=True)

    def results(self, metrics, *, trading_days, elapsed, output_dir):
        if not self.enabled:
            return
        rows = [(label, "N/A" if metrics[name] is None else format(metrics[name], spec))
                for name, label, spec in _METRIC_DISPLAY]
        label_width = max(_width(label) for label, _ in rows)
        value_width = max(4, *(len(value) for _, value in rows))
        border = f"+-{'-' * label_width}-+-{'-' * value_width}-+"
        lines = [f"回测完成 | {trading_days} 个交易日 | 耗时 {elapsed:.2f}s", border]
        for index, (label, value) in enumerate([("指标", "数值"), *rows]):
            lines.append(f"| {label}{' ' * (label_width - _width(label))} "
                         f"| {' ' * (value_width - _width(value))}{value} |")
            if index == 0:
                lines.append(border)
        lines.append(border)
        if any(value is None for value in metrics.values()):
            lines.append("N/A：样本不足、指标未定义或未启用基准。")
        if output_dir is not None:
            lines.append(f"结果目录：{Path(output_dir).resolve()}")
        print("\n".join(lines), flush=True)
