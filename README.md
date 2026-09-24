# skd-backtest

按 [开发规格](BACKTEST_PLATFORM_SPEC_v2.md) 自顶向下搭建的 Python 回测包。
当前版本已实现**按真实交易日逐日推进的顶层回测流程**。10 个模块均有独立边界，
金融算法仍保留待实现注释，并通过 `print` 显示每天经过的位置。

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
    # 当前收到三张列结构完整的空 DataFrame；实际读数将在下一阶段实现。
    print(f"用户 inference: {as_of_date}, 数据集: {list(data)}")
    return pd.DataFrame(columns=["date", "code", "score"])


engine = BacktestEngine(
    data_dir=r"D:\Data",             # 必须显式传入，不硬编码机器路径
    start_date="2024-01-02",         # YYYY-MM-DD，区间两端包含
    end_date="2024-12-31",
    inference=inference,
    initial_cash=1_000_000,
    rebalance_interval=5,            # 调仓间隔，交易日
    holding_period=5,                # RankIC 标签持有期，交易日
    lookback=252,                    # 模型可见历史窗口，交易日
    price_mode="adjusted_return",
    optimizer_config=OptimizerConfig(top_k=50),
    cost_config=CostConfig(slippage=0.0),
)
metrics = engine.run()
print(metrics)                       # 15 项指标，目前全部为 None
print(engine.metrics)                # 同一份指标
print(engine.trading_dates)          # 区间内实际遍历的全部交易日
print(engine.tables["equity_curve"]) # 带固定列名的空审计表
```

已有设计文件中的模型时，只加载一次，再传入其方法句柄：

```python
model = InferenceModel("./submission/model/")
engine = BacktestEngine(
    data_dir=r"D:\Data",
    start_date="2024-01-02",
    end_date="2024-12-31",
    inference=model.predict,
)
metrics = engine.run()
```

句柄必须接受 `as_of_date`、`data` 两个关键字参数，返回 `date/code/score` 三列的
`pandas.DataFrame`。引擎不负责动态导入模型、不重新初始化模型，也不执行训练。
当前真实模型需要能处理空表才能跑演示；示例提供了可直接运行的空推理函数。

## 执行语义

`run()` 从区间涉及的每个月度 MarketData Parquet 文件中只读取“日期”列，
按 `[start_date, end_date]` 过滤、去重并排序，然后遍历**每个实际交易日**。
周末、节假日由数据中的真实日期决定，不使用自然日或普通工作日推算。
每个月的日期列只读一次，不在每日循环中重复加载。

每个交易日按以下顺序调用：

1. 公司行为、开盘市场状态、账户日初处理（预留每日 T+1 解锁）。
2. 若前一交易日生成了目标，调用 Broker 在本日开盘执行，随后清除待执行目标。
3. 收盘估值，包括首日、末日和所有非调仓日。
4. 调仓日收盘提供 as-of 空表，调用 inference 和 Optimizer，目标排到下一交易日开盘。

调仓日从区间内第一个交易日起每隔 `rebalance_interval` 个交易日选取。
末日没有区间内的下一交易日，因此只处理已有目标及估值，不生成新的调仓目标。
只有一个交易日时仍运行日初和估值；没有交易日时跳过每日循环，仍返回完整指标结构。

每日结果先收集，循环结束后一次性合并为审计表，再进行事后 RankIC 评价、
指标计算和结果输出。未来收益标签只供评价器使用，不能进入模型和优化器。
金融模块仍打印 `STUB`：研究表、交易表和估值表为空，尚不撮合、不计算金融指标、
不写结果文件。指标 `None` 表示未计算，不表示收益率为零。

依赖 pandas 和用于读取交易日列的 pyarrow，暂不引入求解器或机器学习依赖。
信任调用者和数据格式，不逐行验证、不重复检查、不捕获和重试错误。
文件读取和 inference 异常直接交给调用者。

## 数据约定

所有路径以构造函数传入的 `data_dir` 为根：

```text
<data_dir>/Factor33_winsor/<YYYY>/<MM>/<YYYYMM>.parquet
<data_dir>/Barra_factor/<YYYY>/<MM>/<YYYYMM>.parquet
<data_dir>/MarketData/<YYYY>/<MM>/<YYYYMM>.parquet
```

`data` 是以 `Factor33_winsor`、`Barra_factor`、`MarketData` 为键的字典，
各表列名保留源数据格式：`日期/代码/名称` 及各自特征列。
后续读数时，源表 `日期` 为整数 YYYYMMDD，代码保留 `SH`/`SZ` 前缀；推理结果
`date` 使用与 `as_of_date` 一致的 YYYY-MM-DD 字符串，`code` 保留市场前缀。
空表本轮只约定列名，不承诺列类型。

依据现有 `D:\Data\README.md`：

- Barra 是每日真实沪深300成分；Factor33 和 MarketData 是当年度成分并集。
  未来合法股票池将以当日 Barra 为准，不能根据年度并集选择股票。
- OHLC 是后复权价；当前文件未提供真实 OHLC/复权因子、涨跌停限价、
  历史基准权重、行业和公司行为数据。默认模式因此是 `adjusted_return`。
- `raw_price` 的接口已保留；严格股数/现金交易实现需补齐上述数据。
  不用复权价冒充真实成交价，也不从 `amount/volume` 推造开盘价。

这些差异不阻碍交易日循环及金融模块占位调用；数据补充布局在实现真实交易前确定。

## 构造配置

必填：`data_dir`、`start_date`、`end_date`、`inference`。
可选：`initial_cash`、`rebalance_interval`、`holding_period`、`lookback`、
`price_mode`、`optimizer_config`、`cost_config`、`trading_days_per_year`、
`risk_free_rate`、`output_dir`。无风险利率按年化小数配置，费率和权重均用小数。

`OptimizerConfig` 包含方法、Top-K、Long Only、Fully Invested、个股权重、主动权重、
行业暴露、Barra 风格暴露和换手限制。`None` 表示该限制未设置。

`CostConfig` 包含佣金率、最低佣金、滑点和按生效日升序排列的 `fee_schedule`。
每条 `FeeScheduleEntry(effective_date, stamp_tax_rate, transfer_fee_rate)` 描述
自该日起生效的费率；当前默认零佣金/零滑点/空费率表仅是骨架参数。

## 模块与后续顺序

| 设计模块 | 文件 | 待实现内容 |
|---|---|---|
| Submission Runner | `submission_runner.py` | 已直连句柄；模型由用户初始化 |
| Data Provider | `data_provider.py` | 已读取真实交易日；待实现研究数据加载、PIT/as-of、缓存 |
| Prediction Evaluator | `prediction_evaluator.py` | 标签、逐日 RankIC |
| Portfolio Optimizer | `portfolio_optimizer.py` | 排名变换、Top-K/tilt、约束优化 |
| Broker / Execution Engine | `broker.py` | T+1、整手/零股、涨跌停、先卖后买、实际账户 |
| Corporate Action Engine | `corporate_actions.py` | 真实价格模式公司行为，避免重复入账 |
| Cost Model | `cost_model.py` | 历史费率、佣金、印花税、过户费、滑点 |
| Portfolio Accounting | `accounting.py` | 每日持仓估值、组合/基准 NAV、日收益 |
| Metrics | `metrics.py` | 预测和组合原始指标 |
| Result Writer | `result_writer.py` | JSON、CSV、运行日志 |

下一步实现 Data Provider 的研究数据与标签读取，再逐个补全预测评价、组合构建、
交易、会计、指标及文件输出。

## 返回指标与审计输出

`engine.run()` 返回扁平字典，`engine.metrics` 保存同一份结果：

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
`orders`、`trades`、`positions`、`equity_curve`。当前只保留 inference 实际返回的
所有调仓日的预测记录，并为 `future_return` 填 `None`，其他表为空。
每日模块输出会统一汇总，后续实现模块时无需修改汇总机制。每次 `run()` 重置运行状态。

Result Writer 预留 `metrics.json`、以上七个 `.csv` 和 `run.log`。
即使指定 `output_dir`，当前也只打印计划输出的文件名，不创建文件或目录。

## 验证与打包

```powershell
python -m unittest discover -s tests -v
uv build
```

也可先安装 `build`，再执行 `python -m build`，生成 wheel 和源码分发包。
测试使用临时月度 Parquet，验证跨月/跨年日期筛选、每日调用顺序、非调仓日估值、
下一开盘执行、末日边界、单日及无交易日区间、审计汇总、重复运行和异常直传。
