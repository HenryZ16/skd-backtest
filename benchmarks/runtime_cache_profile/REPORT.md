# RuntimeCache 性能诊断与优化验证（同步推理基线）

## 结论

本报告记录缓存优化时的同步推理实现；当前默认异步推理的测量见 [异步推理报告](../async_inference/REPORT.md)。

七年完整工作负载的平均耗时从 60.193 秒降至 9.034 秒，减少约 85.0%，速度约为优化前的 6.66 倍。
七张结果表的内容摘要及指标字典与优化前一致。
优化后的耗时仍比接入缓存前的 7.652 秒多约 1.38 秒；目前额外完成阶段交接、每日现金账户记录和协议管理。

另用现有数据流 benchmark 同口径复测，优化后的框架达到纯数据空跑吞吐率的 **75.49%（异步读取）**、
**77.28%（同步读取）**。该比例使用同一基准的耗时中位数，详见“相对空跑的有效带宽”。

原来的主要开销是 CPU 侧 Python/pandas 对象构造、重复校验和历史遍历。
现有证据不支持将主要退化归为大块数组搬运导致的内存带宽瓶颈。
金融算法仍是占位实现，这里的“计算”主要是框架和对象管理。

## 实现与需求对应

- 总设计 153–175 行规定：后复权模式不额外入账公司行为，真实价格模式才显式处理；
  597 行要求只计入一次。总设计没有要求每天重放全部历史事件。
- 后复权模式不加载公司行为源，不逐事件生成跳过记录；公司行为阶段只传递原账户及空事件表。
  真实模式保留当日事件处理协议，缓存用已处理 ID 集合增量去重，不存储空事件历史。
  真实价格金融算法及外部适配器仍待实现。
- 发布和读取直接传递引用，移除递归克隆。发布后生产者与消费者共同遵守只读契约，
  更新时只为改变的表或字段建立新对象，未变部分共享；DataFrame 并非强制只读。
- 固定表结构每次运行首次出现时检查一次，同一表经多个组件流转不重复检查。
  Runner 在首个信号日检查输出结构，每批仍检查分数值、日期和股票池。
  缓存保留阶段、权限、排期、计费身份及公司行为可见时点等动态检查；
  行级业务约束由唯一生产者保证。
- 空目标、成交、持仓和事件表不进入历史；最终七表及完整分数历史各合并一次并复用。
  拼接、筛选及业务更新仍可能分配新表，不将整个 pandas 流程声称为零拷贝。
- 模型研究输入的隔离保留；日志写入时仅复制小型 details，日志读取复用记录。
  basic_usage.py 和只读需求文档未修改。

接口约定见 [interfaces.md](../../docs/interfaces.md)。

## 测量条件

- 工作负载与 basic_usage.py 相同：D:\Data，2016-01-01 至 2022-12-31，
  lookback=252、rebalance_interval=5、read_batch_months=12、异步读取预取、同步模型推理、
  原示例 InferenceModel.predict、output_dir=None。
- 全区间 1,703 个交易日、341 次推理、102,300 条预测。
- 接入缓存前来自提交 94e22c7；current_*.json 记录本次优化前的缓存实现，
  optimized_*.json 记录优化后的实现。工作区测量均保存全部框架源文件 SHA256。
- Python 3.13.13、pandas 3.0.6、pyarrow 25.0.1、psutil 7.2.2，
  Windows 11，32 个逻辑 CPU。完整环境信息保存在各 JSON。
- 每次使用新进程，按顺序测量，不并发争抢 CPU；基线各两次。
  计时覆盖 engine.run()，不含 Python 启动、导入、输出摘要和 JSON 写出。
- RSS 每 50 ms 采样，是进程总占用，可能漏掉瞬时峰值；不是缓存独占内存。
- 未清空系统文件缓存、未锁定 CPU 频率；不是严格受控硬件基准。
  2016 子区间的同步 cProfile 用于定位函数热点，不能直接代表七年时间占比。

## 无 profiler 的完整回测

| 指标 | 接入缓存前 | 优化前缓存 | 优化后缓存 |
|---|---:|---:|---:|
| 两次墙钟耗时 | 7.656 / 7.648 s | 60.201 / 60.186 s | 9.071 / 8.997 s |
| 平均墙钟耗时 | 7.652 s | 60.193 s | 9.034 s |
| 平均主线程 CPU 时间 | 6.539 s | 58.609 s | 7.828 s |
| 平均进程 CPU 时间 | 9.641 s | 62.117 s | 10.617 s |
| 两次 RSS 峰值的平均值 | 587.3 MiB | 755.8 MiB | 602.2 MiB |
| 两次 RSS 时间均值的平均值 | 468.8 MiB | 595.2 MiB | 469.6 MiB |

优化前主线程 CPU 约占墙钟时间 97.4%。
接入缓存前后首轮读取量均为 180,086,537 字节，前台行情等待分别约 0.218 / 0.208 秒，
大量时间没有花在等待磁盘上。

优化后的两次测量均逐一比较七张表的列、行数和内容 SHA256，且与优化前一致；
15 项指标字典也一致。接入缓存前只比较预测内容和指标，
因为旧实现没有每日现金账户交接记录。运行日志的公司行为占位记录按新模式行为减少，不属于金融输出等价范围。

原始数据：[接入前 1](head_full_1.json)、[接入前 2](head_full_2.json)、
[优化前 1](current_full_1.json)、[优化前 2](current_full_2.json)、
[优化后 1](optimized_full_1.json)、[优化后 2](optimized_full_2.json)。

## 相对空跑的有效带宽

“空跑”使用现有 benchmark 的 data 范围：读取每日行情、构造研究窗口、执行相同的窗口检查/信号摘要及零分推理，
并消费每日价格摘要；不调用运行缓存或金融组件。engine 范围运行优化后的完整占位框架。
两者消费者工作并不完全相同，因此该比例表示相对于这项空跑基准的有效吞吐，耗时差不能全部归为缓存自身开销。

同一批 252 个 Parquet 文件，共 158,772,790 字节（151.418 MiB）、1,691,376 行源数据，
覆盖 1,703 个交易日、341 次推理；每个范围和模式预热一次，交替顺序独立进程运行三次，取中位数。
12 次正式运行的交付计数及信号数据摘要一致，源文件内容摘要与历史数据流基线一致。

有效带宽 = 输入 Parquet 文件总字节数 / 2²⁰ / 完整运行耗时，单位 MiB/s。
达到空跑的百分比 = 框架有效带宽 / 空跑有效带宽 × 100%
= 空跑耗时中位数 / 框架耗时中位数 × 100%。
这里衡量端到端数据通路吞吐，不是物理磁盘或 DRAM 带宽；源行数/秒使用同一比例。

| 读取模式 | 空跑耗时中位数 | 框架耗时中位数 | 空跑 MiB/s | 框架 MiB/s | 达到空跑吞吐率 |
|---|---:|---:|---:|---:|---:|
| 同步 | 8.440 s | 10.921 s | 17.94 | 13.86 | **77.28%** |
| 异步读取 | 8.052 s | 10.666 s | 18.80 | 14.20 | **75.49%** |

该基线异步读取的源行吞吐分别为 210,051 行/秒（空跑）和 158,573 行/秒（框架），即 **75.49%**。

本节使用 benchmark_data_flow 的窗口检查和摘要回调，RSS 采样间隔为 10 ms；
前文 9.034 秒使用 basic_usage 的回调和 50 ms 采样，不能把两个实验的耗时混合计算比例。
原始测量见 [优化后的数据流与框架对照](../data_flow_optimized_2016_2022.json)。

复测命令：

~~~powershell
.venv\Scripts\python.exe -B examples/benchmark_data_flow.py --data-dir D:\Data --scope both --repeats 3 --no-async-inference --output benchmarks/data_flow_optimized_recheck.json
~~~

## 同步 cProfile 对照

同为 2016 年、244 个交易日，禁用读取预取、异步推理及内存采样线程。
profiler 自身增加开销，下表时间只用于热点比较，不作为正常回测时间。

| 指标 | 优化前 | 优化后 |
|---|---:|---:|
| profile 总时间 | 13.554 s | 2.180 s |
| 总函数调用 | 35,118,330 | 5,076,875 |
| runtime_cache._clone 累计 | 7.056 s | 已移除 |
| runtime_cache._validate 累计 | 3.608 s | 0.020 s |
| runtime_cache._frame 累计 | 2.653 s | 0.00088 s，15 次 |
| numpy.ndarray.copy 自身 | 0.138 s | 0.0666 s |

_frame 是 _validate 的子调用，不能重复相加。
优化后缓存校验已经不构成主要热点；剩余调用主要是 pandas 构表、类型处理、
Arrow 字符串操作和行情读入。接入缓存前该区间约 367 万次调用，缓存及阶段占位仍有额外对象成本。

原始数据：[接入前](head_cprofile_sync_2016.json)、
[优化前](cprofile_sync_2016.json)、[优化后](optimized_cprofile_sync_2016.json)。
源码位置以各 JSON 中的源文件指纹及函数记录为准，旧 profile 的行号不对应当前实现。

## 完整区间的工作量

| 工作量 | 优化前 | 优化后 |
|---|---:|---:|
| 递归 _clone 调用 | 3,884,212 | 已移除 |
| DataFrame 克隆 | 66,440，其中 56,552 为空表 | 缓存不再克隆表 |
| 缓存表结构检查 | 31,000，其中 27,591 为空表 | 15，其中 11 首次为空表 |
| 发布的公司行为记录 | 0 | 0 |
| 历史事件表回扫 | 1,449,253 | 已移除 |
| 当批事件 ID 检查 | 随历史遍历进行 | 0 |

15 次是缓存的固定结构检查，不包含 Runner 首次输出结构检查或动态业务值检查。
优化前事件扫描恰好为 1703 × 1702 / 2，空事件表仍触发属性访问及 Series 构造。
优化后只在有事件时访问当批 ID 及集合；不再依赖累计历史表数。

优化前被克隆表的底层数组名义体积总和为 125,639,535 B，约 119.8 MiB。
它按 pandas block.values.nbytes 累计，不含对象头、索引和临时数组，
Arrow 共享缓冲区也可能重复计数，不等于实际 DRAM 总流量。

原始计数：[优化前](inventory_full.json)、[优化后](optimized_inventory_full.json)。

## 优化前的独立归因实验

这些实验针对旧复制协议，临时替换一条路径，进程结束恢复；
同区间金融输出均与旧缓存基线一致。单项耗时不能简单相加。

| 单项改变 | 完整七年耗时 | 较 60.193 s 节省 |
|---|---:|---:|
| 仅空表跳过逐对象列处理，保留深拷贝 | 39.614 s | 20.579 s |
| 全部表跳过逐对象列处理，保留深拷贝 | 37.291 s | 22.902 s |
| 无事件日不扫描历史表 | 43.202 s | 16.991 s |
| 仅共享 RunCalendar | 60.169 s | 未显示有意义改善 |

空表没有数据行可搬运，却能省去约 20.6 秒，支持小对象处理是主因的判断。
2016 子区间基线平均 5.870 秒，绕过 _clone 为 2.903 秒，绕过 _frame 为 4.684 秒。
这些临时实验本身没有完成所有权与验证责任调整；正式实现依据本报告的引用协议落地。

原始数据：[空表快路径](empty_frame_fastpath_full.json)、[无对象列重建](no_object_map_full.json)、
[无空事件扫描](no_event_scan_full.json)、[共享日历](share_calendar_full.json)、
[绕过克隆](no_clone_2016.json)、[绕过表检查](no_frame_check_2016.json)、
[一年基线 1](current_2016_1.json)、[一年基线 2](current_2016_2.json)。

## 结论边界

这是软件 profile 与对照实验支持的归因，未测硬件内存带宽、LLC miss 或 CPU stall。
CPU 时间接近墙钟、RSS 较高都不能单独证明 compute-bound 或 memory-bound。
综合空表实验、原生数组复制耗时与历史扫描次数，主要新增成本来自 CPU 侧对象管理；
不排除内存延迟、分配器和缓存未命中的贡献。

该缓存优化版本的 28 项测试通过，包含引用交接、按需复制后旧快照不变、一次结构检查、空历史清理、
公司行为去重及后复权模式不访问事件源。当前无持仓、交易和费用算法，
这些结果不替代未来金融算法的正确性测试和性能测量。

[同进程采样](sample_full.json) 受 GIL 影响，平均采样间隔约 17.5 ms，偏向释放 GIL 的路径；
[最初异步 cProfile](cprofile_2016.json) 的生成器/线程父级时间有交叠，
二者不用于精确热点占比。本报告采用同步 cProfile 和无 profiler 基线。

## 复测

在项目根目录使用现有 .venv 和 benchmark 可选依赖 psutil。
默认数据与模型同 basic_usage.py；新测量写入不同文件，保留已有证据。

~~~powershell
.venv\Scripts\python.exe -B benchmarks/runtime_cache_profile/profile_run.py --no-async-inference --out benchmarks/runtime_cache_profile/recheck_current.json
.venv\Scripts\python.exe -B benchmarks/runtime_cache_profile/profile_run.py --source head --ref 94e22c7 --out benchmarks/runtime_cache_profile/recheck_old.json
.venv\Scripts\python.exe -B benchmarks/runtime_cache_profile/profile_run.py --mode cprofile --sync --no-async-inference --end 2016-12-31 --out benchmarks/runtime_cache_profile/recheck_profile.json
.venv\Scripts\python.exe -B benchmarks/runtime_cache_profile/profile_run.py --mode inventory --no-async-inference --out benchmarks/runtime_cache_profile/recheck_inventory.json
~~~

[诊断脚本](profile_run.py) 保留旧实验名称以解释证据；
依赖已移除克隆/历史扫描路径的实验在当前实现上明确拒绝执行。
临时绕过表检查仍仅供诊断，不能用于回测结论。
