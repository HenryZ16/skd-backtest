# 性能基线

[项目首页](../README.md) · [使用说明](usage.md) · [实现设计](design.md) · [性能基线](benchmarks.md)

以下命令均在项目根目录执行，原始测量报告保存在 `benchmarks/`。

## 数据吞吐基线

安装仅供 benchmark 使用的内存采样依赖（不增加回测包的必需依赖）：

```powershell
python -m pip install -e ".[benchmark]"
```

```powershell
.venv\Scripts\python.exe examples/benchmark_data_flow.py --data-dir D:\Data --output benchmarks/data_flow_2016_2022.json
.venv\Scripts\python.exe examples/benchmark_data_flow.py --data-dir D:\Data --inference-delay-ms 10 --output benchmarks/data_flow_2016_2022_delay10ms.json
```

脚本默认覆盖 `2016-01-01 .. 2022-12-31`，窗口 252 个交易日、每 5 日推理、
每批 12 个月。`--scope both`（默认）分别运行纯数据播放和完整框架；也可选 `data` 或 `engine`。
每个范围、每种读数模式先预热一次，再交替顺序运行 3 次并取中位数。
每次运行使用独立子进程，避免上一轮 Python/Arrow 分配器保留内存影响下一轮的占用；
预热针对操作系统文件缓存，子进程启动和导入耗时不计入运行时间。
记录环境版本、区间内源文件内容摘要、每次原始读数及信号日数据摘要，
校验两个范围、同步/异步的信号日截面和播放计数一致；纯数据消费者另校验逐日价格合计摘要。
完整历史窗口和参考价格语义由自动测试对照源表验证。
可用 `--read-batch-months`、`--lookback`、`--repeats` 调整实验配置。

运行期间以 10 ms 为默认目标间隔采样整个子进程的 RSS（Windows 为 Working Set），
包含已加载的 Python/依赖库、pandas/Arrow 原生分配、后台读取线程及当前结果表，
不包含调度 benchmark 的父进程；测量的是总占用，不是相对于启动时的增量。
采样覆盖引擎构造、一次运行和返回时的清理，不包含输出 JSON 的阶段。
每次结果的 `memory` 保存 `peak_rss_bytes`、`mean_rss_bytes`、采样次数、持续时间和最大实际采样间隔。
峰值是采样点中的最大值；平均值按实际采样间隔做梯形积分后除以持续时间。
可用 `--memory-sample-ms` 调整间隔，短暂尖峰可能被漏采，采样开销计入运行耗时。
汇总的 `peak_rss_mib`、`mean_rss_mib` 分别取各次峰值、平均值的中位数，1 MiB = 1,048,576 字节。
这些指标只加入 benchmark 报告，不改变 `engine.performance` 或模型接口。

本机基线（Windows、Python 3.13.13、pandas 3.0.6、pyarrow 25.0.1、psutil 7.2.2）：
2016-01-04 至 2022-12-30 共 1,703 个交易日、341 次推理；252 个完整数据文件，
三套源表合计 1,691,376 行，其中源行情 590,238 行。开盘和收盘端各交付 803,524 行，
包含已出现但当天缺失的股票保留行，因此交付行数大于源行数。

无附加推理延迟时的中位数：

| 范围 | 模式 | 总耗时 | 前台读取等待 | 交易日/秒 | RSS 采样峰值 | RSS 平均值 |
|---|---|---:|---:|---:|---:|---:|
| 数据播放 | 同步 | 8.213 s | 1.726 s | 207.4 | 585.0 MiB | 410.7 MiB |
| 数据播放 | 异步 | 7.658 s | 0.214 s | 222.4 | 586.5 MiB | 458.3 MiB |
| 完整框架 | 同步 | 8.738 s | 1.713 s | 194.9 | 592.7 MiB | 416.1 MiB |
| 完整框架 | 异步 | 8.246 s | 0.213 s | 206.5 | 589.9 MiB | 463.0 MiB |

两种范围的消费者工作不同，不能把其耗时差直接当作金融模块开销；比较读数模式应在同一范围内进行。
逐次数据见 [无附加延迟报告](../benchmarks/data_flow_2016_2022.json) 和
[10 ms 推理延迟报告](../benchmarks/data_flow_2016_2022_delay10ms.json)。

`engine.performance` 与金融指标分开，包括：

- `elapsed_seconds`：完整引擎运行耗时，含日历准备、读取、播放、推理和金融占位模块。
- `playback_seconds`、`inference_seconds`：数据播放（含日历准备）及用户推理耗时；
  `trading_days`、`market_rows`、`inference_calls` 为交易日、交付开盘行（含缺失行）、推理次数。
- `days_per_second`、`source_rows_per_second`：以完整运行耗时为分母的吞吐。
- `data.calendar_seconds`：日历准备耗时；`data.read_seconds`：批次读取、过滤、排序的累计工作耗时；
  `data.wait_seconds`：前台取得批次的累计等待，包含首批启动等待。
- `data.market_seconds`：开盘/收盘输入切片交付耗时；`open_rows/close_rows`：各端交付行数。
- `data.as_of_seconds`：研究窗口复制耗时；`source_rows`：成分过滤前、含预热的各源表读入行数；
  `delivered_rows`：各次推理窗口累计交付行数，重叠历史会重复计数。
- `seed_files/seed_rows`：独立历史参考价初始化的文件数及扫描行数；其耗时计入 `read_seconds`。
- `data_files`：完整数据文件数；`calendar_files`：另行读取日期列的文件数；
  `data_bytes`：这些完整 Parquet 文件的压缩大小之和，不等于物理磁盘读取量。

后台工作耗时与播放/推理可以重叠，各阶段计时不能简单相加。
基准禁用框架调试日志；子进程的 JSON 结果及每轮摘要均在测量结束后输出。
纯数据范围实际读取每日价格、构造信号日研究窗口并调用
测试推理函数，不调用金融占位模块；完整框架范围包括开盘估值、每日收盘核算等金融占位调用及审计收集，
尚不计算实际市值或收益。
两者均含测试推理中的窗口检查、信号日摘要和可选模拟延迟。纯数据消费者耗时另记为 consumer_seconds。
文件摘要和预热会使操作系统缓存变热，因此这是本机缓存条件下的数据通路基线，
不是冷盘顺序读取速度，也不代表已实现金融算法后的回测速度。
模拟推理使用 sleep，仅衡量可隐藏读取的机会，不代表 CPU 密集型模型的实际加速。
