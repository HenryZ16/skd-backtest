# skd-backtest

面向指数增强研究的日频 Python 回测框架。已实现完整回测链路：
市场数据、独立异步推理、运行缓存、组合构建、交易、费用、估值、标签、评价及审计输出。
组件通过固定缓存协议交换非市场数据，代码按组件独占文件。

默认分别使用后台线程预取数据和顺序执行模型推理，账户在需要分数时等待；
可用 `async_inference=False` 单独关闭推理异步。默认显示交易日进度，并在回测成功后打印指标表；
`friendly_output=False` 时，`engine.run()` 不主动打印进度或结果，命令行评测向标准输出打印指标 JSON。
`engine.run()` 返回 15 项原始指标，
并保留最终账户和七张审计表。指定输出目录时生成指标 JSON、七张 CSV 和运行日志。
优化器支持 Top-K、简单基准倾斜和 Barra 风险优化；正式方法支持主动权重、行业、风格及换手约束。

## 快速开始

需要 Python 3.11 及以上。发行包的 pip 和 uv 安装方式见[使用说明](docs/usage.md)。安装后有两种独立的使用方式：

**Python API（个人单模型回测）**：用户对自己的单个模型进行回测时，建议参考 [examples/basic_usage.py](examples/basic_usage.py) 使用 Python API，将已初始化模型的 `model.predict` 作为 `inference` 传给 `BacktestEngine`。

**命令行评测（评测平台批量回测）**：`skd-backtest-evaluate` 是本包提供的命令行评测入口，推荐用于评测平台批量回测时使用。每次调用处理一个标准提交，由平台调度多个调用，例如 `skd-backtest-evaluate --submission submission --config config.toml`。`skd-backtest-evaluate --help` 同时提供这两种使用方式的说明和完整单模型示例。

从源码开发或运行仓库示例时，在项目根目录执行：

```powershell
python -m pip install -e .
python examples/basic_usage.py
```

[示例](examples/basic_usage.py) 通过 `engine.run()` 运行回测流程，默认读取 `D:\Data`，运行 2016–2022 年区间；
naive 模型为当日全部沪深300成分股统一输出零分，`top_k=300` 生成每只股票 `1/300` 的等权目标。
每5个交易日生成调仓信号，下一交易日开盘执行；实际持仓受停牌等交易限制影响。
运行前请将示例中的 `data_dir` 改为实际数据目录。

`BacktestEngine` 也支持传入 `submission_dir`，由平台加载标准提交并管理每次评测的模型实例。
完整配置、风险数据与可复现性约定见[使用说明](docs/usage.md)。

## 文档

- [使用说明](docs/usage.md)：模型接入、数据格式、API、配置和返回结果。
- [实现设计与开发](docs/design.md)：数据流、模块边界、当前进度、测试与打包。
- [组件接口协议](docs/interfaces.md)：缓存主题、共享数据包、固定入口和并行文件归属。
- [性能基线](docs/benchmarks.md)：复测方法、吞吐与内存结果、指标口径。
- [总设计独立审查](docs/spec_audit.md)：需求覆盖矩阵、返修闭合与独立验证。
- [开发规格](BACKTEST_PLATFORM_SPEC_v2.md)：只读需求文档。

## 许可证

本项目采用 [Apache License 2.0](LICENSE) 许可。
