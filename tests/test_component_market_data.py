"""Real and reconstructed execution prices stay isolated from research data."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

from skd_backtest.config import BacktestConfig, DataCapabilities
from skd_backtest.data_provider import DataProvider
from skd_backtest.schemas import SOURCE_COLUMNS


DAYS = [20201230, 20201231, 20210104, 20210105]
CODE = "SH600000"
RAW_OPEN = [7.0, 8.0, 9.0, 10.0]
RAW_CLOSE = [7.5, 8.5, 9.5, 10.5]
FACTORS = [2.0, 2.0, 3.0, 3.0]


class MarketDataComponentTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write_market(self, root, *, include_raw=True, include_factor=False,
                     include_limits=True, factor_values=None, invalid_factor_date=None):
        factor_values = FACTORS if factor_values is None else factor_values
        by_dataset = {name: [] for name in SOURCE_COLUMNS}
        for index, day in enumerate(DAYS):
            factor = FACTORS[index]
            market = dict.fromkeys(SOURCE_COLUMNS["MarketData"], 1.0)
            market.update({"日期": day, "代码": CODE, "名称": CODE})
            for field, raw in (
                ("open", RAW_OPEN[index]), ("high", RAW_OPEN[index] + 1),
                ("low", RAW_OPEN[index] - 1), ("close", RAW_CLOSE[index]),
            ):
                market[field] = raw * factor
            market["is_suspend"] = False
            if include_raw:
                for field, raw in (
                    ("raw_open", RAW_OPEN[index]), ("raw_high", RAW_OPEN[index] + 1),
                    ("raw_low", RAW_OPEN[index] - 1), ("raw_close", RAW_CLOSE[index]),
                ):
                    market[field] = raw
            if include_factor:
                market["adjustment_factor"] = factor_values[index]
                if day == invalid_factor_date:
                    market["adjustment_factor"] = 0.0
            if include_limits:
                market["upper_limit"] = RAW_OPEN[index] * 1.2
                market["lower_limit"] = RAW_OPEN[index] * 0.8
            by_dataset["MarketData"].append(market)

            for name in ("Factor33_winsor", "Barra_factor"):
                row = dict.fromkeys(SOURCE_COLUMNS[name], 1.0)
                row.update({"日期": day, "代码": CODE, "名称": CODE})
                by_dataset[name].append(row)

        for name, rows in by_dataset.items():
            frame = pd.DataFrame(rows)
            for month, part in frame.groupby(frame["日期"] // 100):
                folder = root / name / str(month)[:4] / str(month)[4:]
                folder.mkdir(parents=True, exist_ok=True)
                part.to_parquet(folder / f"{month}.parquet", index=False)

    def config(self, root, *, price_mode="raw_price", capabilities=None,
               start="2021-01-04", end="2021-01-05", prefetch=False,
               label_price_basis="adjusted_open"):
        return BacktestConfig(
            data_dir=root,
            start_date=start,
            end_date=end,
            initial_cash=1000.0,
            rebalance_interval=1,
            holding_period=1,
            lookback=1,
            price_mode=price_mode,
            trading_days_per_year=252,
            risk_free_rate=0.0,
            output_dir=None,
            read_batch_months=1,
            prefetch=prefetch,
            label_price_basis=label_price_basis,
            data_capabilities=capabilities or DataCapabilities(),
        )

    @staticmethod
    def capture(config):
        frames = []
        provider = DataProvider(config)
        with provider:
            for date in provider.prepare():
                frames.append((
                    date, provider.open_market(date), provider.close_market(date),
                    provider.as_of(date),
                ))
        return frames

    def test_raw_ohlc_uses_real_prices_and_cross_month_raw_reference(self):
        self.write_market(
            self.root, include_raw=True, include_factor=True,
            factor_values=[99.0] * len(DAYS),
        )
        capabilities = DataCapabilities(raw_prices=True, adjustment_factors=True, price_limits=True)
        provider = DataProvider(self.config(self.root, capabilities=capabilities))
        with provider:
            self.assertEqual(provider.prepare(), ["2021-01-04", "2021-01-05"])
            opened = provider.open_market("2021-01-04")
            self.assertEqual(
                set(opened),
                {"date", "code", "raw_open", "upper_limit", "lower_limit", "is_suspended",
                 "is_missing", "previous_close", "previous_close_date"},
            )
            row = opened.iloc[0]
            self.assertEqual(row.raw_open, RAW_OPEN[2])
            self.assertEqual(row.previous_close, RAW_CLOSE[1])
            self.assertEqual(row.previous_close_date, 20201231)
            self.assertNotIn("raw_close", opened)
            self.assertNotIn("high", opened)
            self.assertNotIn("low", opened)
            self.assertNotIn("close", opened)

            closed = provider.close_market("2021-01-04").iloc[0]
            self.assertEqual(closed.raw_close, RAW_CLOSE[2])
            self.assertEqual(closed.reference_close, RAW_CLOSE[2])
            self.assertEqual(closed.previous_close, RAW_CLOSE[1])
            self.assertNotIn("upper_limit", provider.close_market("2021-01-04"))

    def test_factor_mode_divides_adjusted_ohlc_and_valuation_uses_raw_basis(self):
        self.write_market(self.root, include_raw=False, include_factor=True)
        capabilities = DataCapabilities(adjustment_factors=True, price_limits=True)
        provider = DataProvider(self.config(self.root, capabilities=capabilities))
        with provider:
            provider.prepare()
            opened = provider.open_market("2021-01-04").iloc[0]
            closed = provider.close_market("2021-01-04").iloc[0]
            self.assertEqual(opened.raw_open, RAW_OPEN[2])
            self.assertEqual(opened.previous_close, RAW_CLOSE[1])
            self.assertEqual(closed.raw_close, RAW_CLOSE[2])
            self.assertEqual(closed.reference_close, RAW_CLOSE[2])
            valuation = provider.valuation_inputs([CODE, "UNKNOWN"])
            actual = valuation.query("code == @CODE").iloc[0]
            unknown = valuation.query("code == 'UNKNOWN'")
            self.assertEqual(actual.raw_close, RAW_CLOSE[2])
            self.assertEqual(actual.reference_close, RAW_CLOSE[2])
            self.assertTrue(unknown.is_missing.all())
            self.assertTrue(unknown.raw_close.isna().all())

    def test_research_exposes_only_source_columns_and_as_of_mutation_isolated(self):
        self.write_market(self.root, include_raw=True, include_factor=True)
        capabilities = DataCapabilities(raw_prices=True, price_limits=True)
        provider = DataProvider(self.config(self.root, capabilities=capabilities))
        with provider:
            provider.prepare()
            data = provider.as_of("2021-01-04")
            self.assertEqual(set(data), set(SOURCE_COLUMNS))
            for name, columns in SOURCE_COLUMNS.items():
                self.assertEqual(data[name].columns.tolist(), list(columns))
                self.assertEqual(data[name]["日期"].max(), 20210104)
            self.assertNotIn("raw_open", data["MarketData"])
            self.assertNotIn("upper_limit", data["MarketData"])
            self.assertNotIn("adjustment_factor", data["MarketData"])
            data["MarketData"].iloc[0, 0] = 20990101
            self.assertEqual(provider.as_of("2021-01-04")["MarketData"]["日期"].max(), 20210104)

    def test_synchronous_and_prefetched_raw_playback_are_identical(self):
        self.write_market(self.root, include_raw=True)
        capabilities = DataCapabilities(raw_prices=True, price_limits=True)
        synchronous = self.capture(self.config(
            self.root, capabilities=capabilities, start="2020-12-30", prefetch=False,
        ))
        prefetched = self.capture(self.config(
            self.root, capabilities=capabilities, start="2020-12-30", prefetch=True,
        ))
        self.assertEqual(len(synchronous), len(prefetched))
        for sync_day, async_day in zip(synchronous, prefetched):
            self.assertEqual(sync_day[0], async_day[0])
            pd.testing.assert_frame_equal(sync_day[1], async_day[1])
            pd.testing.assert_frame_equal(sync_day[2], async_day[2])
            for name in SOURCE_COLUMNS:
                pd.testing.assert_frame_equal(sync_day[3][name], async_day[3][name])

    def test_missing_factor_limits_and_invalid_market_values_fail_clearly(self):
        cases = (
            ("factor column", dict(include_raw=False, include_factor=False),
             DataCapabilities(adjustment_factors=True, price_limits=True), "adjustment_factor"),
            ("upper limit column", dict(include_raw=True, include_limits=False),
             DataCapabilities(raw_prices=True, price_limits=True), "upper_limit"),
            ("invalid factor", dict(include_raw=False, include_factor=True,
                                    invalid_factor_date=20210104),
             DataCapabilities(adjustment_factors=True, price_limits=True), "positive and finite"),
            ("unordered limits", dict(include_raw=True),
             DataCapabilities(raw_prices=True, price_limits=True), "finite, positive, and ordered"),
        )
        for label, options, capabilities, expected in cases:
            with self.subTest(label=label), TemporaryDirectory() as temp:
                root = Path(temp)
                self.write_market(root, **options)
                if label == "unordered limits":
                    path = root / "MarketData" / "2021" / "01" / "202101.parquet"
                    market = pd.read_parquet(path)
                    market.loc[market["日期"] == 20210104, "lower_limit"] = 100.0
                    market.to_parquet(path, index=False)
                provider = DataProvider(self.config(root, capabilities=capabilities))
                with provider, self.assertRaisesRegex(ValueError, expected):
                    provider.prepare()
                    provider.open_market("2021-01-04")

    def test_missing_price_capability_still_rejects_raw_mode(self):
        self.write_market(self.root, include_raw=True)
        provider = DataProvider(self.config(self.root, capabilities=DataCapabilities()))
        with provider, self.assertRaisesRegex(NotImplementedError, "raw_price"):
            provider.prepare()

    def test_adjusted_execution_stays_adjusted_when_labels_request_raw_open(self):
        self.write_market(self.root, include_raw=True, include_factor=True)
        config = self.config(
            self.root, price_mode="adjusted_return",
            capabilities=DataCapabilities(raw_prices=True, price_limits=True),
            label_price_basis="raw_open",
        )
        provider = DataProvider(config)
        with provider:
            provider.prepare()
            self.assertEqual(provider.open_market("2021-01-04").iloc[0].adjusted_open,
                             RAW_OPEN[2] * FACTORS[2])
            self.assertEqual(provider.close_market("2021-01-04").iloc[0].adjusted_close,
                             RAW_CLOSE[2] * FACTORS[2])


if __name__ == "__main__":
    unittest.main()
