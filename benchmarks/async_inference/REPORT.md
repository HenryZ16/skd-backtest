# 异步推理实现与性能验证

## 实现边界

模型仅依赖当前信号日及以前的研究数据，独立工作线程按信号日期顺序调用模型。
主线程提前准备最多一个调仓间隔的数据，将已有研究窗口交给工作线程，不增加整包复制或进程序列化。
当前与下一信号日合计最多两个推理任务；同一模型不会被多个线程同时调用。
主线程在对应信号阶段等待结果，随后发布分数并调用优化器；账户、交易、缓存推进保持原来的顺序。
未来分数提前计算不会提前发布给金融组件。异常在依赖该结果的信号日传播，取消排队任务，回收运行中的任务。
运行中的用户回调需要自行返回，线程退出会等待它完成。

`prefetch` 控制数据读取预取，`async_inference` 控制模型工作线程，两者独立且默认开启。
轻量模型可设置 `async_inference=False`，沿用同步推理路径。
接口及生命周期见 [接口协议](../../docs/interfaces.md) 和 [实现设计](../../docs/design.md)。

## 测量方法

- 使用未修改的 examples/basic_usage.py 模型与配置：D:\Data，2016-01-01 至 2022-12-31，
  lookback=252、rebalance_interval=5、read_batch_months=12、output_dir=None。
- 1,703 个交易日、341 次推理、102,300 条预测；所有实验启用后台读取，只有模型推理模式和附加延迟不同。
- 模型原本生成零分；延迟组在每次模型调用前 sleep 20 ms，名义附加时间共 6.82 秒。
  这是可重叠延迟实验，不是 XGBoost 或纯 Python 计算密集模型的性能测量。
- 每组运行两次，使用独立进程且不同时测量，报告算术平均。
  运行顺序为 sync0、async0、async0、sync0、sync20、async20、async20、sync20。
- 计时覆盖 engine.run()，不含启动、导入、结果摘要和 JSON 写出；未显式预热，操作系统文件缓存已热。
- Python 3.13.13、pandas 3.0.6、pyarrow 25.0.1、psutil 7.2.2；Windows 11，32 个逻辑 CPU。
  每份 JSON 保存环境、完整框架源文件 SHA256、CPU 时间、数据统计和结果内容摘要。
- RSS 以 50 ms 为目标间隔采样整个进程，峰值可能漏掉短暂尖峰；未控制 CPU 频率或清空系统缓存。

## 完整七年结果

| 每次附加延迟 | 推理方式 | 两次总耗时 | 平均总耗时 | 推理累计经过时间 | 主线程等待推理 | RSS 峰值平均 |
|---|---|---:|---:|---:|---:|---:|
| 0 ms | 同步 | 8.802 / 8.727 s | 8.764 s | 0.576 s | 0 s | 599.5 MiB |
| 0 ms | 异步 | 9.170 / 9.216 s | 9.193 s | 1.761 s | 0.001 s | 594.8 MiB |
| 20 ms | 同步 | 15.404 / 15.308 s | 15.356 s | 7.708 s | 0 s | 586.1 MiB |
| 20 ms | 异步 | 9.262 / 9.265 s | 9.263 s | 7.918 s | 0.871 s | 602.1 MiB |

无附加延迟的模型引入约 0.429 秒开销，总耗时增加 **4.9%**。
每次附加 20 ms 延迟时，异步推理节省约 6.093 秒，总耗时减少 **39.7%**，速度约为同步的 **1.66 倍**。
这符合推理与数据准备、当前账户处理重叠的预期；默认启用异步并不保证所有模型都会变快。

推理累计经过时间包含线程调度和等待运行的时间，不是 CPU 时间；异步模式计时覆盖模型及分数校验，
同步模式还包含向缓存发布分数。轻量模型异步累计经过时间变大不能解释为计算量等比例增加。
同步的主线程等待指标为 0，是因为没有等待 Future，模型执行时间仍计入总耗时。
各线程的经过时间存在重叠，不能相加得到完整回测时间。

这些结果只覆盖金融占位实现。真实模型的 GIL 行为、内部线程竞争及后续金融算法都会影响重叠收益。
受 GIL 限制的纯 Python 推理不能依靠新增线程实现 CPU 并行。

## 正确性验证

32 项自动测试通过，包括同步/异步金融输出一致、两种读取模式、每交易日推理、
用事件握手验证下一信号日推理与当前账户/交易处理重叠、模型串行调用、异常日期归属、
失败后取消排队任务与线程回收、同一引擎失败后重新运行。

八次完整测量的七张结果表列、行数、内容 SHA256 及 15 项指标全部一致，
并与 [缓存优化后的同步基线](../runtime_cache_profile/optimized_full_1.json) 一致。
每份报告的框架源文件摘要均与本次实现一致。金融算法尚未实现，因此当前等价验证不替代后续业务测试。

原始测量：[同步 0 ms 第 1 次](sync_delay0_1.json)、[第 2 次](sync_delay0_2.json)，
[异步 0 ms 第 1 次](async_delay0_1.json)、[第 2 次](async_delay0_2.json)，
[同步 20 ms 第 1 次](sync_delay20_1.json)、[第 2 次](sync_delay20_2.json)，
[异步 20 ms 第 1 次](async_delay20_1.json)、[第 2 次](async_delay20_2.json)。

## 与空跑带宽记录的关系

[缓存优化基线](../../docs/benchmarks.md) 的 75.49% / 77.28% 是同步模型推理下，
异步读取 / 同步读取相对于纯数据空跑的吞吐比例。
该实验使用 benchmark_data_flow 的检查/摘要回调和 10 ms 内存采样；本实验使用原 basic_usage 回调与 50 ms 采样。
不混用两次实验的耗时计算比例，也不将旧比例标为当前异步推理的性能。

## 复测

在项目根目录执行，每条命令独立运行两次并更换输出文件名，交替顺序比较。
去掉 `--inference-delay-ms 20` 可测无附加延迟的原示例模型。

```powershell
.venv\Scripts\python.exe -B benchmarks/runtime_cache_profile/profile_run.py --no-async-inference --inference-delay-ms 20 --out benchmarks/async_inference/recheck_sync20.json
.venv\Scripts\python.exe -B benchmarks/runtime_cache_profile/profile_run.py --async-inference --inference-delay-ms 20 --out benchmarks/async_inference/recheck_async20.json
```

标准数据流 benchmark 也支持 `--async-inference` / `--no-async-inference`；
其 sync/async 结果标签仍表示读取方式。需要分析单线程调用热点时，禁用两种后台任务：

```powershell
.venv\Scripts\python.exe -B benchmarks/runtime_cache_profile/profile_run.py --mode cprofile --sync --no-async-inference --end 2016-12-31 --out benchmarks/async_inference/recheck_profile.json
```
