import tempfile
import unittest
from pathlib import Path

import pandas as pd

from skd_backtest.config import BacktestConfig, DataCapabilities, ReferenceSources
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


def make_config(*, mode="csi300", returns=None, weights=None, industries=None, capabilities=None):
    return BacktestConfig(
        data_dir=".", start_date="2024-01-02", end_date="2024-01-04",
        initial_cash=1000.0, rebalance_interval=1, holding_period=1, lookback=1,
        price_mode="adjusted_return", trading_days_per_year=252, risk_free_rate=0.0,
        output_dir=None, benchmark_mode=mode,
        data_capabilities=capabilities or DataCapabilities(
            benchmark_returns=returns is not None,
            benchmark_weights=weights is not None,
            industries=industries is not None,
        ),
        reference_sources=ReferenceSources(
            benchmark_returns=returns, benchmark_weights=weights, industries=industries,
        ),
    )


def market_context(date="2024-01-03"):
    universe = pd.DataFrame({"code": ["000001.SZ", "600000.SH"]})
    barra = pd.DataFrame({"date": [date, date], "code": universe.code, "beta": [0.2, 0.3]})
    return MarketContext(date, universe, barra)


class ReferenceDataProviderTests(unittest.TestCase):
    def test_csv_and_parquet_sources_publish_exact_date_slices_and_share_market_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            returns_path = root / "returns.csv"
            weights_path = root / "weights.parquet"
            industries_path = root / "industries.csv"
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
                returns=returns_path, weights=weights_path, industries=industries_path,
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

    def test_unconfigured_sources_are_unavailable_and_benchmark_none_does_not_read_them(self):
        market = market_context()
        cache = MemoryCache(market)
        csi_provider = ReferenceDataProvider(make_config(mode="csi300"))
        csi_provider.prepare_close(date=market.date, cache=cache)
        self.assertEqual(cache.values[Topic.REFERENCE_BENCHMARK, market.date].status, "unavailable")

        provider = ReferenceDataProvider(make_config(mode="none"))
        provider.prepare_close(date=market.date, cache=cache)
        provider.prepare_signal(date=market.date, cache=cache)
        self.assertEqual(cache.values[Topic.REFERENCE_BENCHMARK, market.date].reason, "benchmark_mode=none")
        inputs = cache.values[Topic.REFERENCE_PORTFOLIO, market.date]
        self.assertEqual(inputs.benchmark_weights.status, "unavailable")
        self.assertEqual(inputs.industries.status, "unavailable")

        missing = Path(tempfile.gettempdir()) / "must-not-be-read-reference.parquet"
        provider = ReferenceDataProvider(make_config(
            mode="none", returns=missing, weights=missing,
            capabilities=DataCapabilities(),
        ))
        provider.prepare_close(date=market.date, cache=cache)
        provider.prepare_signal(date=market.date, cache=cache)
        self.assertEqual(cache.values[Topic.REFERENCE_BENCHMARK, market.date].reason, "benchmark_mode=none")
        self.assertEqual(cache.values[Topic.REFERENCE_PORTFOLIO, market.date].benchmark_weights.status, "unavailable")

    def test_declared_missing_file_or_date_fails_instead_of_filling(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.csv"
            cache = MemoryCache()
            with self.assertRaises(FileNotFoundError):
                ReferenceDataProvider(make_config(returns=missing)).prepare_close(
                    date="2024-01-03", cache=cache,
                )

            path = Path(directory) / "returns.csv"
            pd.DataFrame([("2024-01-02", 0.01)], columns=["date", "benchmark_return"]).to_csv(path, index=False)
            with self.assertRaisesRegex(ValueError, "no data for 2024-01-03"):
                ReferenceDataProvider(make_config(returns=path)).prepare_close(
                    date="2024-01-03", cache=cache,
                )

    def test_weights_and_industries_must_cover_the_signal_universe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            weights = root / "weights.csv"
            industries = root / "industries.csv"
            pd.DataFrame([
                ("2024-01-03", "000001.SZ", 1.0),
            ], columns=["date", "code", "benchmark_weight"]).to_csv(weights, index=False)
            pd.DataFrame([
                ("2024-01-03", "000001.SZ", "Technology"),
            ], columns=["date", "code", "industry"]).to_csv(industries, index=False)
            cache = MemoryCache(market_context())

            with self.assertRaisesRegex(ValueError, "cover the legal universe"):
                ReferenceDataProvider(make_config(weights=weights)).prepare_signal(
                    date="2024-01-03", cache=cache,
                )
            with self.assertRaisesRegex(ValueError, "industries are missing codes"):
                ReferenceDataProvider(make_config(industries=industries)).prepare_signal(
                    date="2024-01-03", cache=cache,
                )

    def test_market_wide_industries_are_filtered_to_the_legal_universe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "industries.csv"
            pd.DataFrame([
                ("2024-01-03", "000001.SZ", "Technology"),
                ("2024-01-03", "600000.SH", "Financials"),
                ("2024-01-03", "999999.SZ", "Other"),
            ], columns=["date", "code", "industry"]).to_csv(path, index=False)
            provider = ReferenceDataProvider(make_config(industries=path))
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
            ("weights", [("2024-01-02", "000001.SZ", 0.4), ("2024-01-02", "600000.SH", 0.5)],
             ["date", "code", "benchmark_weight"], "sum to one"),
            ("industries", [("2024-01-02", "000001.SZ", None)],
             ["date", "code", "industry"], "nonempty strings"),
        )
        for kind, rows, columns, message in cases:
            with self.subTest(kind=kind, message=message), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / f"{kind}.csv"
                pd.DataFrame(rows, columns=columns).to_csv(path, index=False)
                config = make_config(**{kind: path})
                provider = ReferenceDataProvider(config)
                cache = MemoryCache(market_context("2024-01-02"))
                with self.assertRaisesRegex(ValueError, message):
                    if kind == "returns":
                        provider.prepare_close(date="2024-01-02", cache=cache)
                    else:
                        provider.prepare_signal(date="2024-01-02", cache=cache)

    def test_weights_date_slice_is_exact_and_requires_same_day_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "weights.parquet"
            pd.DataFrame([
                ("2024-01-02", "000001.SZ", 0.4), ("2024-01-02", "600000.SH", 0.6),
            ], columns=["date", "code", "benchmark_weight"]).to_parquet(path, index=False)
            provider = ReferenceDataProvider(make_config(weights=path))
            with self.assertRaisesRegex(ValueError, "no data for 2024-01-03"):
                provider.prepare_signal(date="2024-01-03", cache=MemoryCache(market_context()))


if __name__ == "__main__":
    unittest.main()