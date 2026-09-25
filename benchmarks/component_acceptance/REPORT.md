# 组件初验与性能基线

## 实现范围

十个组件分别由独占文件的 Luna 6 max subagent 开发，主任务负责接口协调、代码审查、修正复核与联合验收。
本目录性能记录对应正式优化器与独立审查返修接入前的组件初验快照；其源码指纹保存在 profile_final.json。
当前实现已增加标准提交、Barra 风险约束、随机状态管理并修正停牌参考价；完整接口以 docs 为准。
市场数据播放、异步推理和缓存接口保持兼容；`examples/basic_usage.py` 与 HEAD 逐字节一致。

| 组件文件 | 已验收行为 |
|---|---|
| data_provider.py | 后复权/真实 OHLC、复权因子恢复、历史参考价、限价输入，研究输入仍使用原字段 |
| reference_data.py | CSV/Parquet 基准收益、权重、行业；精确日期、合法池对齐，每次运行释放与重新读取 |
| portfolio_optimizer.py | Top-K、简单 benchmark tilt、并列排名、单股上限、现金余量、退出持仓 |
| broker.py | 下一开盘执行、先卖后买、T+1、整手/零股、单边限价、停牌、现金缩量 |
| cost_model.py | 生效日费率、最低佣金、卖出印花税、过户费、方向滑点 |
| accounting.py | 两种价格模式的开盘/收盘估值、现金与资产、逐日净值/收益、实际权重 |
| label_provider.py | 独立读取全市场交易日，计算 Open(t+H+1)/Open(t+1)-1，保留缺失标签及原因 |
| prediction_evaluator.py | 全部分数按键对齐标签，平均并列排名的逐日 Spearman RankIC |
| metrics.py | 15 项原始指标，样本标准差、年化、回撤、基准与缺失值语义 |
| result_writer.py | 严格 JSON、七张 CSV、运行日志，失败清理本次完成标记 |

本次历史测量范围对应只读需求的“第一阶段开发范围”，使用 Top-K。
当前已实现在此之后要求的 Barra 风险模型及行业/风格/主动权重/换手约束；现货执行不支持卖空。基准数据必须来自声明的外部来源，不生成等权替代；历史费率由配置提供，默认空费率表为零税费。

## 正确性与交付检查

- `python -m unittest discover -s tests -v`：**108 项通过**。
- 联调使用真实组件和手算数据，覆盖从目标到成交、现金、持仓、净值、标签、指标及九个输出文件的完整链路。
- 无费用的手算后复权账户按 1000 → 1100 → 1200 → 1320 变化，总收益 32%；真实价格场景独立检查成交股数和 T+1 锁定。
- 含费用和滑点的联调验证现金与资产分别记账，最低佣金边界和大比例订单现金缩量有专项回归。
- 同步/异步读取 × 同步/异步推理共四种组合，七张审计表、账户及指标一致。
- 覆盖空区间、单日、重复运行后外部源重读、旧快照不变、无未来数据、后台异常及线程回收。
- 输出验证包括固定字段、JSON 非有限数转 null、已有文件保护、写入/发布/最终日志关闭失败时移除完成标记。
- `uv build --offline --python .venv/Scripts/python.exe`：源码包与 wheel 构建通过，使用本地缓存的隔离构建依赖。
- 只读需求文件 SHA256 保持 `4D0575A88E4FA245B05C9D198BE42C6515780FD8CF8CE44B16896F87D89C1F1D`。

原版示例完整运行 1,703 个交易日、341 次推理，单次耗时 **20.408 s**，返回实际金融指标；
原始记录见 [basic_usage.json](basic_usage.json)。常数分数没有 RankIC 方差，未配置基准时相关指标为 null，
均为定义明确的缺失结果。该单次 smoke 不与下文 benchmark 混算吞吐比例。

## 组件初验性能

使用 D:\Data 的 2016–2022 年区间，lookback=252、每 5 日推理、每批 12 个月、无附加模型延迟、output_dir=None。
252 个源文件合计 158,772,790 字节（151.418 MiB）、1,691,376 行源数据。
12 次正式运行的交付计数和信号日摘要一致；数据内容指纹见 [data_flow.json](data_flow.json)。

每个范围/读取模式先预热一次，交替顺序在独立进程中重复三次，以下均为中位数。
完整回测默认开启独立异步推理；表中的同步/异步仅指行情读取。
纯数据范围直接调用相同的窗口检查、信号摘要与零分回调，不经过金融组件，也不创建模型线程。

| 读取模式 | 空跑耗时 | 完整回测耗时 | 空跑 MiB/s | 回测 MiB/s | 达到空跑吞吐率 |
|---|---:|---:|---:|---:|---:|
| 同步读取 | 8.553 s | 22.711 s | 17.70 | 6.67 | **37.66%** |
| 异步读取（默认） | 8.060 s | 22.338 s | 18.79 | 6.78 | **36.08%** |

有效带宽 = 上述源文件字节数 / 2²⁰ / 完整运行耗时；
达到空跑的比例 = 空跑耗时中位数 / 完整回测耗时中位数。
这是固定输入规模下的端到端等效吞吐，**不是物理磁盘或 DRAM 带宽**。
回测耗时还包括标签的独立事后读取与计算，但分子固定为播放输入文件的逻辑字节数，不重复计入标签读取。

| 读取模式 | 空跑 RSS 峰值 / 均值 | 回测 RSS 峰值 / 均值 | 回测前台读数等待 |
|---|---:|---:|---:|
| 同步读取 | 587.0 / 416.2 MiB | 627.4 / 438.5 MiB | 1.810 s |
| 异步读取（默认） | 591.1 / 463.8 MiB | 629.1 / 495.5 MiB | 0.211 s |

RSS 是整个子进程的总占用，目标每 10 ms 采样；峰值可能漏掉短暂尖峰，均值按实际间隔积分。
上表分别取各次峰值/均值的中位数。数据已预热，不代表冷盘速度。
完整回测默认异步读取时约 76.24 交易日/秒、75,717 源行/秒。
纯数据与完整回测消费逻辑不同，耗时差不能全部归因于缓存；
此前占位阶段的 75.49%/77.28% 不代表当前已实现金融计算的速度。

## 函数热点

该初验快照另用原示例模型运行一次 cProfile；为覆盖模型调用，关闭异步推理，保留后台行情读取。
原始记录和源文件 SHA256 见 [profile_final.json](profile_final.json)。
profiler 本身增加开销，本次 37.490 s 不能与无 profiler 的示例或吞吐基准直接比较。

| 普通函数累计时间 | 调用数 | 时间 |
|---|---:|---:|
| Accounting._mark_account | 3406 | 15.001 s |
| 其中 _market_rows | 3403 | 6.840 s |
| LabelProvider.build | 1 | 1.520 s |
| RuntimeCache.publish | 41842 | 0.661 s |

父子累计时间不可相加；生成器、上下文管理器与线程路径的父级累计时间存在交叠，不用来计算占比。
持仓估值只转换所持股票的行情，空持仓跳过转换，避免每日构造全市场 Python 记录。
该快照可见的主要热点是估值与 pandas/对象处理，缓存发布本身占用较小。
主线程 CPU 时间 35.703 s、进程 CPU 时间 40.141 s；这些软件计时不能独立判定硬件 memory-bound/compute-bound，
没有测量 DRAM 流量、缓存未命中或停顿周期，不作硬件瓶颈结论。

## 复现

在项目根目录，保留已有测量文件，将新结果写入新的路径：

~~~powershell
.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.venv\Scripts\python.exe -B examples/basic_usage.py
.venv\Scripts\python.exe -B examples/benchmark_data_flow.py --data-dir D:\Data --scope both --repeats 3 --output benchmarks/component_acceptance/data_flow_recheck.json
.venv\Scripts\python.exe -B benchmarks/runtime_cache_profile/profile_run.py --mode cprofile --no-async-inference --out benchmarks/component_acceptance/profile_recheck.json
uv build --offline --python .venv/Scripts/python.exe
~~~

测量环境为 Windows 11、Python 3.13.13、pandas 3.0.6、pyarrow 25.0.1、psutil 7.2.2、32 个逻辑 CPU。
未固定 CPU 频率或清空操作系统文件缓存；benchmark 的检查/摘要回调不代表实际 XGBoost 模型开销。
