"""Native reference ingestion, normalization and exact historical coverage."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

from skd_backtest.config import OptimizerConfig
from skd_backtest.contracts import Topic
from skd_backtest.reference_data import ReferenceDataProvider
from test_component_reference import MemoryCache, make_config, market_context


class NativeReferenceTests(unittest.TestCase):
    def native(self, root, *, snapshot=20231229, codes=("000001.SZ", "600000.SH"), values=(60., 40.)):
        path = root / "HS300_weight" / "2024" / "hs300_weight_2024.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"日期": [20240103] * 2, "代码": codes,
                      "名称": ["银行", "另一银行"], "权重": values,
                      "权重来源": ["天软预估", "估算补齐"], "指数成份日": [snapshot] * 2}).to_csv(
                          path, encoding="gbk", index=False)
        return path

    def test_native_weights_normalize_and_preserve_provided_constituents(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.native(root, values=(59.988, 40.))
            original = path.read_bytes()
            for source in (root,):
                with self.subTest(source=source):
                    provider = ReferenceDataProvider(make_config(mode="none", data_dir=source))
                    cache = MemoryCache(market_context())
                    provider.prepare_signal(date="2024-01-03", cache=cache)
                    actual = cache.values[Topic.REFERENCE_PORTFOLIO, "2024-01-03"].benchmark_weights.data
                    self.assertEqual(actual.code.tolist(), ["000001.SZ", "600000.SH"])
                    self.assertAlmostEqual(actual.benchmark_weight.iloc[0], 59.988 / 99.988)
                    self.assertAlmostEqual(actual.benchmark_weight.iloc[1], 40. / 99.988)
                    self.assertEqual(actual.source.tolist(), ["天软预估", "估算补齐"])
                    self.assertTrue(actual.snapshot_date.eq("2023-12-29").all())
            self.assertEqual(path.read_bytes(), original)

    def test_native_industries_use_historical_codes_across_files_and_name_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for day, label, code, encoding in (
                (20231229, "申万化工", "SW801030", "gbk"),
                (20240103, "申万基础化工", "SW801030", "utf-8-sig"),
                (20250103, "申万电子", "SW801080", "gbk"),
            ):
                path = root / "HS300_industry" / str(day // 10000) / f"hs300_industry_{day // 10000}.csv"
                path.parent.mkdir(parents=True)
                pd.DataFrame({"日期": [day] * 2, "代码": ["000001.SZ", "600000.SH"],
                    "名称": ["示例一", "示例二"], "行业名称": [label, "申万银行"],
                    "行业代码": [code, "SW801780"]}).to_csv(path,encoding=encoding,index=False)
                paths.append(path)
            originals = [path.read_bytes() for path in paths]
            for source in [root]:
                provider = ReferenceDataProvider(make_config(mode="none", data_dir=source))
                slices = provider._load("industries")
                dates = list(slices) if source == root else [next(iter(slices))]
                for date in dates:
                    cache = MemoryCache(market_context(date))
                    provider.prepare_signal(date=date, cache=cache)
                    actual = cache.values[Topic.REFERENCE_PORTFOLIO, date].industries.data
                    expected = "SW801080" if date.startswith("2025") else "SW801030"
                    self.assertEqual(actual.industry.tolist(), [expected, "SW801780"])
                with self.assertRaisesRegex(ValueError, "no data for 2024-01-02"):
                    provider._external("industries", "2024-01-02")
            self.assertEqual([path.read_bytes() for path in paths], originals)

    def test_future_snapshot_and_future_record_are_not_usable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.native(root, snapshot=20240131)
            with self.assertRaisesRegex(ValueError, "snapshot date must not be after"):
                ReferenceDataProvider(make_config(data_dir=root))._load("benchmark_weights")
            self.native(root)
            provider = ReferenceDataProvider(make_config(data_dir=root))
            with self.assertRaisesRegex(ValueError, "no data for 2024-01-02"):
                provider.prepare_signal(date="2024-01-02", cache=MemoryCache(market_context("2024-01-02")))

    def test_mismatched_weights_empty_directory_and_duplicate_records_fail(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "HS300_weight").mkdir()
            with self.assertRaisesRegex(ValueError, "no CSV or Parquet"):
                ReferenceDataProvider(make_config(data_dir=root))._load("benchmark_weights")
            path = self.native(root, codes=("000001.SZ", "OLD"))
            with self.assertRaisesRegex(ValueError, "cover the legal universe exactly"):
                ReferenceDataProvider(make_config(data_dir=root)).prepare_signal(
                    date="2024-01-03", cache=MemoryCache(market_context()))
            self.native(root, codes=("000001.SZ", "000001.SZ"))
            with self.assertRaisesRegex(ValueError, "duplicate keys"):
                ReferenceDataProvider(make_config(data_dir=root))._load("benchmark_weights")

    def test_required_directories_follow_selected_features(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(mode="none", data_dir=root)
            config.validate_data(OptimizerConfig())
            with self.assertRaisesRegex(ValueError, "HS300_weight"):
                config.validate_data(OptimizerConfig(method="barra"))
            self.native(root)
            config.validate_data(OptimizerConfig(method="barra"))
            with self.assertRaisesRegex(ValueError, "HS300_industry"):
                config.validate_data(OptimizerConfig(method="barra", industry_exposure_limit=.02))
            with self.assertRaisesRegex(ValueError, "HS300_return"):
                replace(config, benchmark_mode="csi300").validate_data(OptimizerConfig(method="barra"))


if __name__ == "__main__":
    unittest.main()
