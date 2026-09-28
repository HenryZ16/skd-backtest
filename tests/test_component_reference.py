import tempfile
import unittest
from pathlib import Path

import pandas as pd

from skd_backtest.config import BacktestConfig, REFERENCE_DATASETS
from skd_backtest.contracts import BenchmarkDay, MarketContext, Topic
from skd_backtest.reference_data import ReferenceDataProvider


class MemoryCache:
    def __init__(self, market=None):
        self.values = {}
        if market is not None:
            self.values[Topic.MARKET_CONTEXT, market.date] = market

    def read(self, topic, key=None):
        return self.values[topic, key]

    def publish(self, topic, key, value):
        self.values[topic, key] = value


def make_config(*, data_dir=".", mode="csi300"):
    return BacktestConfig(
        data_dir=data_dir, start_date="2024-01-02", end_date="2024-01-04",
        initial_cash=1000.0, rebalance_interval=1, holding_period=1, lookback=1,
        price_mode="adjusted_return", trading_days_per_year=252, risk_free_rate=0.0,
        output_dir=None, benchmark_mode=mode,
    )


def source_path(root, kind, suffix="csv"):
    name = {"returns": "benchmark_returns", "weights": "benchmark_weights", "industries": "industries"}[kind]
    path = root / REFERENCE_DATASETS[name] / f"data.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def market_context(date="2024-01-03"):
    universe = pd.DataFrame({"code": ["000001.SZ", "600000.SH"]})
    barra = pd.DataFrame({"date": [date, date], "code": universe.code, "beta": [0.2, 0.3]})
    return MarketContext(date, universe, barra)


class ReferenceDataProviderTests(unittest.TestCase):
    def test_csv_and_parquet_sources_publish_exact_date_slices_and_share_market_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            returns_path = source_path(root, "returns")
            weights_path = source_path(root, "weights", "parquet")
            industries_path = source_path(root, "industries")
            pd.DataFrame([
                ("2024-01-02", 0.01), ("2024-01-03", 0.02),
            ], columns=["date", "benchmark_return"]).to_csv(returns_path, index=False)
            pd.DataFrame([
                ("2024-01-02", "000001.SZ", 0.4), ("2024-01-02", "600000.SH", 0.6),
                ("2024-01-03", "000001.SZ", 0.4), ("2024-01-03", "600000.SH", 0.6),
            ], columns=["date", "code", "benchmark_weight"]).to_parquet(weights_path, index=False)
            pd.DataFrame([
                ("2024-01-03", "000001.SZ", "Technology"),
                ("2024-01-03", "600000.SH", "Financials"),
            ], columns=["date", "code", "industry"]).to_csv(industries_path, index=False)

            market = market_context()
            original_universe = market.universe.copy(deep=True)
            original_barra = market.barra_exposures.copy(deep=True)
            cache = MemoryCache(market)
            provider = ReferenceDataProvider(make_config(
                data_dir=root,
            ))

            provider.prepare_close(date="2024-01-03", cache=cache)
            benchmark = cache.values[Topic.REFERENCE_BENCHMARK, "2024-01-03"]
            self.assertEqual(benchmark.data, BenchmarkDay("2024-01-03", 0.02))

            provider.prepare_signal(date="2024-01-03", cache=cache)
            first = cache.values[Topic.REFERENCE_PORTFOLIO, "2024-01-03"]
            self.assertIs(first.universe, market.universe)
            self.assertIs(first.barra_exposures.data, market.barra_exposures)
            self.assertEqual(first.benchmark_weights.data.code.tolist(), ["000001.SZ", "600000.SH"])
            self.assertEqual(first.industries.data.industry.tolist(), ["Technology", "Financials"])

            provider.prepare_signal(date="2024-01-03", cache=cache)
            second = cache.values[Topic.REFERENCE_PORTFOLIO, "2024-01-03"]
            self.assertIs(first.benchmark_weights.data, second.benchmark_weights.data)
            self.assertIs(first.industries.data, provider._slices["industries"]["2024-01-03"])
            pd.testing.assert_frame_equal(market.universe, original_universe)
            pd.testing.assert_frame_equal(market.barra_exposures, original_barra)

            updated = pd.read_csv(returns_path)
            updated.loc[updated.date == "2024-01-03", "benchmark_return"] = 0.09
            updated.to_csv(returns_path, index=False)
            provider.prepare_close(date="2024-01-03", cache=cache)
            self.assertEqual(cache.values[Topic.REFERENCE_BENCHMARK, "2024-01-03"].data.benchmark_return, 0.02)
            provider.close()
            provider.prepare_close(date="2024-01-03", cache=cache)
            self.assertEqual(cache.values[Topic.REFERENCE_BENCHMARK, "2024-01-03"].data.benchmark_return, 0.09)

    def test_weights_are_normalized_per_date_without_rewriting_the_source(self):
        rows = [
            ("2024-01-02", "000001.SZ", .59988), ("2024-01-02", "600000.SH", .4),
            ("2024-01-03", "000001.SZ", .60011), ("2024-01-03", "600000.SH", .4),
            ("2024-01-04", "000001.SZ", 0.), ("2024-01-04", "600000.SH", .8),
        ]
        source = pd.DataFrame(rows, columns=["date", "code", "benchmark_weight"])
        for suffix in ("csv", "parquet"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                path = source_path(Path(directory), "weights", suffix)
                getattr(source, "to_" + suffix)(path, index=False)
                original = path.read_bytes()
                provider = ReferenceDataProvider(make_config(data_dir=directory))
                for date, expected in (("2024-01-02", .59988 / .99988),
                                       ("2024-01-03", .60011 / 1.00011),
                                       ("2024-01-04", 0.)):
                    cache = MemoryCache(market_context(date))
                    provider.prepare_signal(date=date, cache=cache)
                    weights = cache.values[Topic.REFERENCE_PORTFOLIO, date].benchmark_weights.data
                    self.assertAlmostEqual(weights.benchmark_weight.sum(), 1.)
                    self.assertAlmostEqual(weights.benchmark_weight.iloc[0], expected)
                self.assertEqual(path.read_bytes(), original)

    def test_missing_directories_and_disabled_benchmark(self):
        with tempfile.TemporaryDirectory() as directory:
            market = market_context()
            cache = MemoryCache(market)
            with self.assertRaisesRegex(ValueError, "HS300_return"):
                ReferenceDataProvider(make_config(data_dir=directory)).prepare_close(date=market.date, cache=cache)
            # Even an invalid return file is ignored when index evaluation is disabled.
            path = source_path(Path(directory), "returns")
            path.write_text("invalid", encoding="utf-8")
            provider = ReferenceDataProvider(make_config(data_dir=directory, mode="none"))
            provider.prepare_close(date=market.date, cache=cache)
            provider.prepare_signal(date=market.date, cache=cache)
            self.assertEqual(cache.values[Topic.REFERENCE_BENCHMARK, market.date].reason, "benchmark_mode=none")
            inputs = cache.values[Topic.REFERENCE_PORTFOLIO, market.date]
            self.assertEqual(inputs.benchmark_weights.status, "unavailable")
            self.assertEqual(inputs.industries.status, "unavailable")

    def test_missing_file_or_date_fails_instead_of_filling(self):
        with tempfile.TemporaryDirectory() as directory:
            path = source_path(Path(directory), "returns")
            cache = MemoryCache()
            with self.assertRaisesRegex(ValueError, "no CSV or Parquet"):
                ReferenceDataProvider(make_config(data_dir=directory)).prepare_close(date="2024-01-03", cache=cache)
            pd.DataFrame([("2024-01-02", 0.01)], columns=["date", "benchmark_return"]).to_csv(path, index=False)
            with self.assertRaisesRegex(ValueError, "no data for 2024-01-03"):
                ReferenceDataProvider(make_config(data_dir=directory)).prepare_close(date="2024-01-03", cache=cache)

    def test_weights_and_industries_require_complete_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            weights = source_path(root / "weights_case", "weights")
            industries = source_path(root / "industries_case", "industries")
            pd.DataFrame([
                ("2024-01-03", "000001.SZ", 1.0),
            ], columns=["date", "code", "benchmark_weight"]).to_csv(weights, index=False)
            pd.DataFrame([
                ("2024-01-03", "000001.SZ", "Technology"),
            ], columns=["date", "code", "industry"]).to_csv(industries, index=False)
            cache = MemoryCache(market_context())

            with self.assertRaisesRegex(ValueError, "cover the legal universe exactly"):
                ReferenceDataProvider(make_config(data_dir=root / "weights_case")).prepare_signal(
                    date="2024-01-03", cache=cache,
                )
            with self.assertRaisesRegex(ValueError, "industries are missing codes"):
                ReferenceDataProvider(make_config(data_dir=root / "industries_case")).prepare_signal(
                    date="2024-01-03", cache=cache,
                )

    def test_market_wide_industries_are_filtered_to_the_legal_universe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = source_path(Path(directory), "industries")
            pd.DataFrame([
                ("2024-01-03", "000001.SZ", "Technology"),
                ("2024-01-03", "600000.SH", "Financials"),
                ("2024-01-03", "999999.SZ", "Other"),
            ], columns=["date", "code", "industry"]).to_csv(path, index=False)
            provider = ReferenceDataProvider(make_config(data_dir=directory))
            cache = MemoryCache(market_context())
            provider.prepare_signal(date="2024-01-03", cache=cache)
            inputs = cache.values[Topic.REFERENCE_PORTFOLIO, "2024-01-03"]
            self.assertEqual(set(inputs.industries.data.code), {"000001.SZ", "600000.SH"})
            self.assertEqual(len(provider._slices["industries"]["2024-01-03"]), 3)

    def test_source_validation_rejects_bad_dates_types_keys_and_values(self):
        cases = (
            ("returns", [("2024-1-2", 0.01)], ["date", "benchmark_return"], "YYYY-MM-DD"),
            ("returns", [("2024-01-02", float("inf"))], ["date", "benchmark_return"], "finite"),
            ("returns", [("2024-01-02", -1.01)], ["date", "benchmark_return"], "at least -1"),
            ("returns", [("2024-01-02",)], ["date"], "missing required columns"),
            ("returns", [("2024-01-02", 0.01), ("2024-01-02", 0.02)],
             ["date", "benchmark_return"], "duplicate keys"),
            ("weights", [("2024-01-02", "000001.SZ", -0.1), ("2024-01-02", "600000.SH", 1.1)],
             ["date", "code", "benchmark_weight"], "nonnegative"),
            ("weights", [("2024-01-02", "000001.SZ", 0.0), ("2024-01-02", "600000.SH", 0.0)],
             ["date", "code", "benchmark_weight"], "positive finite sum"),
            ("weights", [("2024-01-02", "000001.SZ", 1e308), ("2024-01-02", "600000.SH", 1e308)],
             ["date", "code", "benchmark_weight"], "positive finite sum"),
            ("industries", [("2024-01-02", "000001.SZ", None)],
             ["date", "code", "industry"], "nonempty strings"),
        )
        for kind, rows, columns, message in cases:
            with self.subTest(kind=kind, message=message), tempfile.TemporaryDirectory() as directory:
                path = source_path(Path(directory), kind)
                pd.DataFrame(rows, columns=columns).to_csv(path, index=False)
                config = make_config(data_dir=directory)
                provider = ReferenceDataProvider(config)
                cache = MemoryCache(market_context("2024-01-02"))
                with self.assertRaisesRegex(ValueError, message):
                    if kind == "returns":
                        provider.prepare_close(date="2024-01-02", cache=cache)
                    else:
                        provider.prepare_signal(date="2024-01-02", cache=cache)

    def test_weights_date_slice_is_exact_and_requires_same_day_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = source_path(Path(directory), "weights", "parquet")
            pd.DataFrame([
                ("2024-01-02", "000001.SZ", 0.4), ("2024-01-02", "600000.SH", 0.6),
            ], columns=["date", "code", "benchmark_weight"]).to_parquet(path, index=False)
            provider = ReferenceDataProvider(make_config(data_dir=directory))
            with self.assertRaisesRegex(ValueError, "no data for 2024-01-03"):
                provider.prepare_signal(date="2024-01-03", cache=MemoryCache(market_context()))


if __name__ == "__main__":
    unittest.main()