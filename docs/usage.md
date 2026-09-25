# 使用说明

[项目首页](../README.md) · [使用说明](usage.md) · [实现设计](design.md) · [性能基线](benchmarks.md)

以下命令均在项目根目录执行。市场数据流、运行缓存和公共接口已实现；金融组件保留可运行的占位实现。
当前示例通过 engine.run() 验证完整流程，金融指标仍为 None，表示尚未计算。

## 安装与运行

Python 3.11 及以上，在项目根目录执行：

```powershell
python -m pip install -e .
python examples/basic_usage.py
```

使用 uv 也可以：

```powershell
uv venv .venv
uv pip install -e .
.venv\Scripts\python.exe examples/basic_usage.py
```

```python
import pandas as pd
from skd_backtest import BacktestEngine, CostConfig, OptimizerConfig


def inference(as_of_date, data):
    # data 只包含截至当日、最多 lookback 个交易日的研究历史。
    day = int(as_of_date.replace("-", ""))
    codes = data["Barra_factor"].loc[lambda frame: frame["日期"] == day, "代码"]
    return pd.DataFrame({"date": as_of_date, "code": codes, "score": 0.0})


engine = BacktestEngine(
    data_dir=r"D:\Data",             # 必须显式传入，不硬编码机器路径
    start_date="2016-01-01",         # YYYY-MM-DD，区间两端包含
    end_date="2022-12-31",
    inference=inference,
    initial_cash=1_000_000,
    rebalance_interval=5,            # 调仓间隔，交易日
    holding_period=5,                # RankIC 标签持有期，交易日
    lookback=252,                    # 模型可见历史窗口，交易日
    read_batch_months=12,            # 每批最多 12 个自然月
    prefetch=True,                   # 后台预取下一批行情
    async_inference=True,            # 独立推理线程；False 使用同步推理
    price_mode="adjusted_return",
    optimizer_config=OptimizerConfig(top_k=50),
    cost_config=CostConfig(slippage=0.0),
)
metrics = engine.run()
print(metrics)                       # 15 项指标，目前全部为 None
print(engine.metrics)
print(engine.trading_dates)          # 实际遍历的全部交易日
print(engine.tables["predictions"]) # 保留模型分数，future_return 尚未计算
print(engine.performance)           # 读取、播放、推理及框架耗时
```

已有设计文件中的模型时，只加载一次，再传入其方法句柄：

```python
model = InferenceModel("./submission/model/")
engine = BacktestEngine(
    data_dir=r"D:\Data",
    start_date="2016-01-01",
    end_date="2022-12-31",
    inference=model.predict,
)
metrics = engine.run()
```

句柄必须接受 `as_of_date`、`data` 两个关键字参数，返回 `date/code/score` 三列的
`pandas.DataFrame`。Runner 会验证当日日期、有限数值分数、代码唯一及完整覆盖当日合法池，
然后按代码排序并发布缓存。引擎不负责动态导入模型、不重新初始化模型，也不执行训练。
示例按当日 Barra 成分返回零分，仅演示数据通路。历史不足时提供已有数据，模型自行处理短窗口。

默认模型在独立线程按信号日期顺序调用，可在账户处理当前调仓期间计算下一信号日。
模型始终只接收对应日期的研究数据；账户在 Optimizer 需要当日分数时等待，缓存只由主线程访问。
最多提前缓冲一个调仓间隔的日包，研究窗口按引用交给任务，不新增深拷贝。
`async_inference=False` 恢复同步推理，`prefetch=False` 只关闭行情读取预取；两者独立。
用户回调必须正常返回或抛出异常才能结束正在运行的线程。无需修改 model.predict 签名或 basic_usage 调用方式。

## 数据约定

所有路径以构造函数传入的 `data_dir` 为根：

```text
<data_dir>/Factor33_winsor/<YYYY>/<MM>/<YYYYMM>.parquet
<data_dir>/Barra_factor/<YYYY>/<MM>/<YYYYMM>.parquet
<data_dir>/MarketData/<YYYY>/<MM>/<YYYYMM>.parquet
```

`data` 是以 `Factor33_winsor`、`Barra_factor`、`MarketData` 为键的字典，
各表列名保留源数据格式：`日期/代码/名称` 及各自特征列。
源表 `日期` 为整数 YYYYMMDD，代码保留 `SH`/`SZ` 前缀，按日期、代码排序；
NaN 保持不变，不补值、不重新缩尾或拟合变换。推理结果的 `date` 使用
YYYY-MM-DD 字符串，`code` 保留市场前缀。

引擎专用行情保留年度并集，以便后续继续处理已调出成分的持仓：
开盘列为 `date/code/adjusted_open/is_suspended/is_missing/previous_close/previous_close_date`。
收盘列为 `date/code/adjusted_close/is_suspended/is_missing/has_valid_close/previous_close/`
`previous_close_date/reference_close/reference_date/is_stale`。

- `previous_close`：严格早于当日的最近有限正收盘价；首条记录之前没有已知价格则为空。
- `reference_close`：截至当日的最近有限正收盘价；缺失、NaN、Inf、非正价格不覆盖历史参考价。
- 参考价格日期使用整数 YYYYMMDD，可空；`date` 是 YYYY-MM-DD 字符串。
- `is_missing`：源表当天没有这只股票的记录；`has_valid_close`：当日源记录包含有限正收盘价。
- `is_stale`：有参考价格，但其源记录日期早于当日。停牌源数据本身已有前向填充，
  因此参考日期表示源表记录日期，不保证是最近真实成交日期；`is_suspended` 独立保留。

引擎行情会保留已出现但当前缺失的股票；当日开盘/收盘原值保持空缺，历史参考价单独提供，
不填成可成交价格、不把缺失当零收益。股票跨年退出源数据覆盖时也不会从行情输入中消失。
新出现的股票不会因为批次预取而提前出现在开盘/收盘输入中。
开盘接口不携带当日 high/low/close/volume/amount；后复权价也不命名为 raw_open。
基准权重和行业数据缺失时，通过 Dataset(status="unavailable", data=None, reason=...) 表达，
不使用空表或等权假冒真实数据。DailyData.portfolio 仅承载当日 Barra，其他参考输入由 Reference Data 发布。

依据现有 `D:\Data\README.md`：

- Barra 是每日真实沪深300成分；Factor33 和 MarketData 是当年度成分并集。
  研究输入已按逐日 Barra 成分过滤，当前合法股票池由当日 Barra 定义。
- 财务因子的披露日 PIT 和源数据预处理依赖数据生产方；本层只按已有日期和成分过滤，
  不从日频因子文件重新构建财报披露版本。
- OHLC 是后复权价；当前文件未提供真实 OHLC/复权因子、涨跌停限价、
  历史基准权重、行业和公司行为数据。默认模式因此是 `adjusted_return`。
- `raw_price` 配置名保留，但当前运行会明确抛出 `NotImplementedError`；
  严格股数/现金交易需补齐真实价格等数据，不从 `amount/volume` 推造开盘价。

当前完成数据播放、研究输入、运行缓存与公共接口；真实交易、标签、外部参考数据适配和金融指标仍需后续实现。

## 独立数据 API

`engine.data_provider.playback()` 不调用模型、Broker、Accounting、Metrics 或 ResultWriter，
按真实交易日依次交付 `DailyData`。消费者可以选择需要的字段：

```python
from contextlib import closing

with closing(engine.data_provider.playback()) as days:
    for day in days:
        open_prices = day.open_market
        close_prices = day.close_market
        # 非信号日以及末日为 None；不在数据 API 内自动调用 inference。
        research = day.research
        portfolio = day.portfolio
        next_date = day.execution_date
```

完整遍历时自动回收线程；提前 break 或消费者可能抛出异常时使用 `closing`，确保及时回收。
独立调用从头播放；若已调用 prepare，则复用该准备结果而不重复启动读取。
关闭后下一次重新准备。不支持在同一个 DataProvider 上交错运行两个播放迭代器。

独立加载整个回测区间的估值输入：

```python
valuation_data = engine.data_provider.valuation_inputs()
selected = engine.data_provider.valuation_inputs(codes=["SZ000001", "SH600000"])
```

一次调用返回逐日收盘输入表，不计算收益。只读取 MarketData，不要求因子和 Barra 文件；
使用独立读数状态，不干扰当前播放或修改其统计。传入 codes 时，未知或缺失股票也保留每日一行，
价格未知则为空，不补造历史价格。省略 codes 时返回源数据中已出现过的股票。
无交易日区间返回带完整列名的空表。
此 API 会物化整个区间的结果，内存随日期数和股票数增长；播放 API 的分批缓存限制不适用于返回表。

## 构造配置

必填：`data_dir`、`start_date`、`end_date`、`inference`。
可选：`initial_cash`、`rebalance_interval`、`holding_period`、`lookback`、
`price_mode`、`optimizer_config`、`cost_config`、`trading_days_per_year`、
`risk_free_rate`、`output_dir`、`read_batch_months`（默认 12）、`prefetch`（默认 True）、
`async_inference`（默认 True），
以及 `benchmark_mode`（默认 none）、`label_price_basis`（默认 adjusted_open）、
`rights_policy`（默认 skip）、`data_capabilities` 和 `reference_sources`。
无风险利率按年化小数配置，费率和权重均用小数。

DataCapabilities 和 ReferenceSources 可从 skd_backtest 导入，分别声明数据能力和外部来源路径。
配置为冻结数据类；已有默认源支持后复权研究数据、停牌、成分及 Barra。
启用 csi300 需要基准日收益；基准相对优化和相应约束还需要权重、行业或暴露。
能力声明不替代真实数据检查，也不会使尚未实现的文件适配器自动可用。
完整字段及其关系见[接口协议](interfaces.md)。

`OptimizerConfig` 包含方法、Top-K、Long Only、Fully Invested、个股权重、主动权重、
行业暴露、Barra 风格暴露和换手限制。`None` 表示该限制未设置。

`CostConfig` 包含佣金率、最低佣金、滑点和按生效日升序排列的 `fee_schedule`。
每条 `FeeScheduleEntry(effective_date, stamp_tax_rate, transfer_fee_rate)` 描述
自该日起生效的费率；当前默认零佣金/零滑点/空费率表仅是骨架参数。

## 返回指标与审计输出

`engine.run()` 返回以下扁平字典，`engine.metrics` 保存该结果。
当前占位实现的 15 项指标全部为 None，表示尚未计算，不表示收益率为零。
数据、模型或协议发生真实错误时仍会抛出异常，并保持 metrics=None、tables/account 为空。

| 键 | 规格指标 |
|---|---|
| `mean_rankic` | Mean RankIC |
| `rankic_std` | RankIC Std |
| `rankic_ir` | RankICIR（不年化） |
| `positive_rankic_ratio` | Positive RankIC Ratio |
| `total_return` | Total Return |
| `annualized_return` | Annualized Return |
| `annualized_excess_return` | Annualized Excess Return |
| `annualized_volatility` | Annualized Volatility |
| `maximum_drawdown` | Maximum Drawdown |
| `tracking_error` | Tracking Error |
| `information_ratio` | Information Ratio |
| `sharpe_ratio` | Sharpe Ratio |
| `turnover` | Turnover |
| `transaction_cost` | Transaction Cost |
| `failed_orders` | Failed Orders |

`engine.tables` 包含规格的七张审计表：`predictions`、`rankic`、`target_weights`、
`orders`、`trades`、`positions`、`equity_curve`。缓存已按唯一生产者归集这些表：分数和标签由 Evaluator 合并，目标来自 Optimizer，
订单/成交来自 Broker，持仓/净值来自 Accounting。Engine 只在完整运行及资源清理成功后
接收输出对象的所有权，每次 run 重置运行状态。内部缓存使用只读约定下的共享引用，关闭时不修改导出的对象。

当前 predictions 保留各信号日的模型分数，future_return 为 None；target_weights、orders、trades、
positions 和 rankic 为空。equity_curve 每日有一行现金账户的交接记录，NAV、收益、换手及成本字段
尚未计算，保持 None。账户仍为初始现金、无持仓。

Result Writer 已实现 run.log，指定 output_dir 后在读取日历之前开始记录配置、阶段及错误，
失败时也刷新并关闭。若目录已有协议输出文件（含 run.log），直接拒绝，需选择新目录。
不配置 output_dir 时不写文件。metrics.json 和七张 CSV 的最终序列化尚未实现，
write 记录 STUB 并发布 disabled 回执，继续返回内存占位结果；指定目录时当前只生成 run.log。
