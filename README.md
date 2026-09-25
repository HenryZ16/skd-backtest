# skd-backtest

面向指数增强研究的日频 Python 回测框架。已实现逐日市场数据流、运行缓存、
公共接口和组件接线；后续业务组件可按独占文件并行实现。
默认分别使用后台线程预取数据和顺序执行模型推理，账户在需要分数时等待；可用 `async_inference=False` 单独关闭推理异步。
交易、估值和金融指标算法保留可运行的占位实现；`engine.run()` 可跑通完整流程，
金融指标目前仍为 `None`。

## 快速开始

需要 Python 3.11 及以上，在项目根目录执行：

```powershell
python -m pip install -e .
python examples/basic_usage.py
```

[示例](examples/basic_usage.py) 通过 `engine.run()` 运行流程骨架，默认读取 `D:\Data`，运行 2016–2022 年区间；
运行前请将示例中的 `data_dir` 改为实际数据目录。

接入自己的模型时，将已初始化模型的 `model.predict` 作为 `inference` 传给
`BacktestEngine`。完整示例与接口约定见[使用说明](docs/usage.md)。

## 文档

- [使用说明](docs/usage.md)：模型接入、数据格式、API、配置和返回结果。
- [实现设计与开发](docs/design.md)：数据流、模块边界、当前进度、测试与打包。
- [组件接口协议](docs/interfaces.md)：缓存主题、共享数据包、固定入口和并行文件归属。
- [性能基线](docs/benchmarks.md)：复测方法、吞吐与内存结果、指标口径。
- [开发规格](BACKTEST_PLATFORM_SPEC_v2.md)：只读需求文档。
