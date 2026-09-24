# skd-backtest

面向指数增强研究的日频 Python 回测框架。当前已实现数据读取与逐日播放，
交易、估值和金融指标算法仍在开发中。

## 快速开始

需要 Python 3.11 及以上，在项目根目录执行：

```powershell
python -m pip install -e .
python examples/basic_usage.py
```

[示例](examples/basic_usage.py) 默认读取 `D:\Data`，运行 2016–2022 年区间；
运行前请将示例中的 `data_dir` 改为实际数据目录。

接入自己的模型时，将已初始化模型的 `model.predict` 作为 `inference` 传给
`BacktestEngine`。完整示例与接口约定见[使用说明](docs/usage.md)。

## 文档

- [使用说明](docs/usage.md)：模型接入、数据格式、API、配置和返回结果。
- [实现设计与开发](docs/design.md)：数据流、模块边界、当前进度、测试与打包。
- [性能基线](docs/benchmarks.md)：复测方法、吞吐与内存结果、指标口径。
- [开发规格](BACKTEST_PLATFORM_SPEC_v2.md)：只读需求文档。
