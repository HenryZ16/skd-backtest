# 使用说明

[项目首页](../README.md) · [使用说明](usage.md) · [实现设计](design.md) · [性能基线](benchmarks.md)

以下命令均在项目根目录执行。完整回测链路已实现；示例通过 engine.run() 计算交易、账户、标签和评价指标。

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
print(metrics)                       # 15 项原始指标；未定义值为 None
print(engine.metrics)
print(engine.trading_dates)          # 实际遍历的全部交易日
print(engine.tables["predictions"]) # 模型分数与独立计算的 future_return
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
然后按代码排序并发布缓存。传入回调时，模型实例由调用者创建与管理；平台不执行训练。
示例按当日 Barra 成分返回零分，仅演示数据通路。历史不足时提供已有数据，模型自行处理短窗口。

默认模型在独立线程按信号日期顺序调用，可在账户处理当前调仓期间计算下一信号日。
模型始终只接收对应日期的研究数据；账户在 Optimizer 需要当日分数时等待，缓存只由主线程访问。
最多提前缓冲一个调仓间隔的日包，研究窗口按引用交给任务，不新增深拷贝。
`async_inference=False` 恢复同步推理，`prefetch=False` 只关闭行情读取预取；两者独立。
用户回调必须正常返回或抛出异常才能结束正在运行的线程。无需修改 model.predict 签名或 basic_usage 调用方式。

## 标准参赛提交与可复现性

用 `submission_dir="./submission"` 替代 `inference=model.predict` 即可由平台加载
`submission/inference.py`，调用 `InferenceModel(model_dir)`；model/ 可以有多个固化模型文件。
两个参数必须且只能传一个。每次评测只初始化一次实例，各信号日复用；
同一个 Engine 再次 run 时，标准提交会重新加载模型，避免上次评测的内部状态影响结果。
推理文件可用 `from .helper import ...` 加载提交目录内的辅助模块，不同队伍的相对导入互不混用。

`random_seed` 默认 0，控制加载模型与推理期间的 Python random 和 NumPy 传统全局随机流；
每个 Runner 持有独立状态，调用结束恢复宿主进程随机状态，模型调用之间继续推进自己的流。
可复现评测需固定 submission、数据、配置与数值环境。自行创建的 `default_rng()`、第三方随机生成器、
外部熵及非确定性硬件算法由参赛者固定种子/配置；平台不自动改写参赛代码。
直接传 callable 时，平台重置上述随机流，但其自有模型内部状态仍由调用者复位或重新创建。

统一入口使用 JSON 或 TOML 配置：

~~~powershell
python evaluate.py --submission submissions/team_001 --config configs/private.toml
# 安装包也提供同样的命令：
skd-evaluate --submission submissions/team_001 --config configs/private.toml
~~~

最小 TOML：

~~~toml
[backtest]
data_dir = "D:/Data"
start_date = "2016-01-01"
end_date = "2022-12-31"
random_seed = 0
output_dir = "../result/team_001"

[optimizer]
method = "top_k"
top_k = 50
~~~

配置段为 backtest、optimizer、costs、data_capabilities、reference_sources；
字段与对应数据类同名。相对数据和输出路径相对于配置文件目录解析。
统一入口未指定 output_dir 时使用 `result/<提交目录名>`，必定生成审计文件。
费用表使用 costs.fee_schedule 条目，包含 effective_date、stamp_tax_rate、transfer_fee_rate。
直接使用 Engine 时仍可 output_dir=None，仅返回内存结果。

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

- `previous_close`：严格早于当日、明确非停牌记录中的最近有限正收盘价；首条记录之前没有已知价格则为空。
- `reference_close`：截至当日、明确非停牌记录中的最近有限正收盘价；停牌、状态缺失、缺价、NaN、Inf、非正价格不覆盖历史参考价。
- 参考价格日期使用整数 YYYYMMDD，可空；`date` 是 YYYY-MM-DD 字符串。
- `is_missing`：源表当天没有这只股票的记录；`has_valid_close`：当日明确非停牌且包含有限正收盘价。
- `is_stale`：有参考价格，但其有效源记录日期早于当日。停牌行即使携带有限报价也不会更新参考价；
  跨批次和回测前历史初始化使用同一规则，`is_suspended` 独立保留。

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
  历史基准权重和行业数据。默认模式因此是 `adjusted_return`。
- `raw_price` 已支持真实股数、T+1、整手买入和涨跌停单边限制；须补齐以下数据，
  不从 `amount/volume` 推造开盘价。

真实价格模式在同一月度 MarketData 中额外读取：

| 能力声明 | 必需附加列 |
|---|---|
| `raw_prices=True` | `raw_open/raw_high/raw_low/raw_close` |
| `adjustment_factors=True` 且没有 raw_prices | `adjustment_factor` |
| `price_limits=True` | `upper_limit/lower_limit` |

复权因子约定为 `adjusted_price = raw_price * adjustment_factor`，因子必须为有限正值。
同时提供原价和因子时优先原价。真实开盘/收盘接口分别使用 raw_open/raw_close，
previous_close 和 reference_close 也使用原价体系。研究窗口始终只含原 SOURCE_COLUMNS，
不会把原价、因子或限价附加列传给模型。股票停牌或当日价格缺失时不成交，持仓继续按最近可用参考价估值。

`label_price_basis` 独立选择 adjusted_open 或 raw_open；后者同样需要原价或因子能力，
但不要求将交易模式切换为 raw_price。

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

必填：`data_dir`、`start_date`、`end_date`，以及 `inference` / `submission_dir` 二选一。
可选：`initial_cash`、`rebalance_interval`、`holding_period`、`lookback`、
`price_mode`、`optimizer_config`、`cost_config`、`trading_days_per_year`、
`risk_free_rate`、`output_dir`、`read_batch_months`（默认 12）、`prefetch`（默认 True）、
`async_inference`（默认 True）、`random_seed`（默认 0），
以及 `benchmark_mode`（默认 none）、`label_price_basis`（默认 adjusted_open）、
`data_capabilities` 和 `reference_sources`。
无风险利率按年化小数配置，费率和权重均用小数。

DataCapabilities 和 ReferenceSources 可从 skd_backtest 导入，分别声明数据能力和外部来源路径。
配置为冻结数据类；已有默认源支持后复权研究数据、停牌、成分及 Barra。
启用 csi300 需要基准日收益；基准相对优化和相应约束还需要权重、行业或暴露。
能力声明不替代真实数据检查。外部参考来源支持 CSV 或 Parquet，按精确日期读取，不前向填充。
完整字段及其关系见[接口协议](interfaces.md)。

`OptimizerConfig.method` 支持 top_k、benchmark_tilt 和 barra。Top-K 按分数降序、代码升序选择，
股票数不足 K 时使用全部合法股票。benchmark_tilt 以历史基准权重乘
`0.5 + score_percentile` 后归一化；并列分数使用平均排名，全部同分时保持基准权重。

三种方法均支持 Fully Invested 和 single_name_weight_limit。
Fully Invested=True 时无法满仓会报错；False 允许持有剩余现金。
现货引擎不支持卖空，long_only=False 明确报错。`None` 表示限制未设置。
股票调出合法池时生成零目标，实际能否卖出由 Broker 判断。

### 正式 Barra 优化器

安装可选依赖 `python -m pip install -e ".[optimizer]"`，选择
`OptimizerConfig(method="barra", risk_aversion=1.0, ...)`。
默认十个 barra_factors 沿用 Barra 源表的风格列名，也可指定非空且不重复的因子名元组。

正式方法使用信号日暴露矩阵 X、因子协方差 F 和个股特异方差 D，计算
`Σ = X F Xᵀ + diag(D)`。在配置约束下最大化
`alphaᵀ w - risk_aversion / 2 × (w-b)ᵀ Σ (w-b)`，
其中 alpha 为平均并列百分位减 0.5，b 为历史基准权重。
风险数据由数据生产方以当日已知的 PIT 估计提供；平台不伪造协方差、不用事后收益估计当日风险。
协方差与特异方差必须使用相同的收益周期和单位，risk_aversion 按该单位配置。

| 约束配置 | 正式方法含义 |
|---|---|
| active_weight_limit | 每股 `abs(w-b)` 上限 |
| industry_exposure_limit | 每行业主动权重和的绝对值上限 |
| barra_style_exposure_limit | 每个配置因子的主动暴露 `abs(Xᵀ(w-b))` 上限 |
| turnover_limit | `sum(abs(w-current_weights))` 上限，包含调出股票归零的卖出部分，不乘 1/2 |

高级约束必须搭配 method=barra；简单方法收到这些选项时明确报错。
约束为非负小数，0 表示精确限制；满仓与换手限制必须共同可行，
例如初始全现金到满仓的双边股票权重换手至少为 1。实际成交受交易限制影响，可偏离优化目标。

需配置 benchmark_mode=csi300，并在 DataCapabilities / ReferenceSources 启用对应能力和路径：

| 外部来源 | 每个信号日必需字段 |
|---|---|
| benchmark_weights | date、code、benchmark_weight |
| factor_covariance | date、factor1、factor2、covariance |
| specific_risk | date、code、specific_variance |
| industries（启用行业限制时） | date、code、industry |

另需 benchmark_returns 提供每个回测日的基准收益。外部来源均支持 CSV/Parquet，date 为 YYYY-MM-DD。
因子协方差需给出配置因子的完整成对矩阵，包括双向项及零项；要求对称、半正定。
特异方差需覆盖合法股票池且非负，暴露来自当日 Barra 源表。缺少当日来源或不可行约束明确失败。
求解采用 SciPy 的线性可行性检查和 [SLSQP](https://docs.scipy.org/doc/scipy/reference/optimize.minimize-slsqp.html)，
结果再验证约束，约束容差为 1e-8。不自动放宽用户约束或回退简单算法。

`CostConfig` 包含佣金率、最低佣金、滑点和按生效日升序排列的 `fee_schedule`。
每条 `FeeScheduleEntry(effective_date, stamp_tax_rate, transfer_fee_rate)` 描述
自该日起生效的费率。默认零佣金、零滑点、空费率表表示显式使用零费用配置；
程序不内置或自动获取历史税率。非空费率表未覆盖某交易日期时会报错。
佣金按成交现金对价乘费率与最低佣金的较大者收取，印花税仅卖出，过户费双边收取。
买入滑点提高现金对价，卖出滑点降低现金对价；transaction_cost 只汇总显式费用，避免重复计算滑点。
现金不足时缩量并重新报价，只有最终接受的报价计入成交。

## 返回指标与审计输出

`engine.run()` 返回以下扁平字典，`engine.metrics` 保存该结果。
None 表示没有有效样本、比率未定义或未启用基准。示例的模型分数全部相同，因此 RankIC 相关指标为 None；组合收益仍正常计算。
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

predictions 保留所有信号日分数与独立计算的未来收益；缺价、停牌或数据集尾部不足时，
future_return 为空，不删除预测记录。RankIC 使用有效配对的平均并列排名计算，不受是否成交影响。
标签只在事后读取，可越过回测 end_date 取得持有期终点，模型不会收到未来价格。

target_weights 记录目标，orders/trades 记录真实执行结果，positions/equity_curve 记录实际持仓与逐日账户。
目标不等于实际持仓；受限订单不会自动顺延。后复权模式用资产金额记账，股数与真实成交价字段为空；
真实模式按原价和股数核算，并维护可卖股数和 T+1 锁定。

标准差采用样本标准差，RankICIR 不年化。年化收益以全部回测交易日数计算；
最大回撤包含初始 NAV=1，按非负损失比例表示；年化超额收益为组合年化收益减基准年化收益。
turnover 是每日双边成交现金对价 / 当日开盘交易前权益的合计；failed_orders 只计完全拒绝，
部分成交由订单表记录。完整公式见[接口协议](interfaces.md)。

Result Writer 在指定 output_dir 后，从运行开始记录配置、阶段及错误。
成功时输出 metrics.json、七张 CSV 和 run.log；空表也保留完整列，JSON 空值为 null。
已有任一协议输出文件的目录会被拒绝，需选择新目录；output_dir=None 只保留内存结果。
CSV 先写入临时文件，全部成功后最后发布 metrics.json 作为完成标记。
写入失败不发布成功回执，也不保留本次完成标记；日志和已写 CSV 可用于排错。
