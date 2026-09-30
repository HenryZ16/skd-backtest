"""Rounding, historical board rules, and isolated raw-open limit estimates."""

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from skd_backtest.adjusted_limits import AdjustedLimitProvider, _limit_rate, _price_limits


class AdjustedLimitTest(unittest.TestCase):
    def test_exchange_rounding_is_half_up_in_raw_cents(self):
        # 10.05 * 1.1 = 11.055; 10.05 * .9 = 9.045: both half cents round up.
        upper, lower = _price_limits(22.12, 11.06, 20.10, Decimal("0.10"))
        self.assertAlmostEqual(upper, 22.12)
        self.assertAlmostEqual(lower, 18.10)
        # Division used to recover the reference must not leak float error at a tie.
        upper, lower = _price_limits(40.922, 11.06, 37.184999999999995, Decimal("0.10"))
        self.assertAlmostEqual(upper, 40.922)
        self.assertAlmostEqual(lower, 33.485)
        # Low prices must move at least one cent, with a one-cent price floor.
        self.assertEqual(_price_limits(.04, .04, .04, Decimal("0.10")), (.05, .03))
        self.assertEqual(_price_limits(.01, .01, .01, Decimal("0.10")), (.02, .01))

    def test_today_factor_handles_a_change_without_using_today_close(self):
        # Yesterday adjusted close 20; today's factor 4 implies reference 5.
        self.assertEqual(_price_limits(22.0, 5.5, 20.0, Decimal("0.10")), (22.0, 18.0))

    def test_board_prefixes_and_chinext_effective_date(self):
        for day, code, st, expected in (
            ("2019-08-01", "SH688001", 0, "0.20"),
            ("2024-01-02", "SH688001", 1, "0.20"),
            ("2020-08-21", "SZ300001", 0, "0.10"),
            ("2020-08-21", "SZ300001", 1, "0.05"),
            ("2020-08-24", "SZ300001", 0, "0.20"),
            ("2020-08-24", "SZ300001", 1, "0.20"),
            ("2024-01-02", "SZ301001", 0, "0.20"),
            ("2025-01-02", "SZ302132", 0, "0.20"),
            ("2024-01-02", "SH600000", 0, "0.10"),
            ("2024-01-02", "SZ000001", 1, "0.05"),
        ):
            with self.subTest(day=day, code=code, st=st):
                self.assertEqual(_limit_rate(day, code, st), Decimal(expected))
        with self.assertRaisesRegex(ValueError, "is_st"):
            _limit_rate("2024-01-02", "SH600000", float("nan"))
        with self.assertRaisesRegex(ValueError, "unsupported"):
            _limit_rate("2024-01-02", "UNKNOWN", 0)

    def test_month_cache_key_alignment_and_missing_inputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for month in ("202401", "202402"):
                for dataset, frame in (
                    ("MarketDataRawOpen", pd.DataFrame({"日期": [int(month+"02")]*2,
                        "代码": ["SZ000001", "SH600000"], "open": [9.05, 11.06]})),
                    ("Factor33_winsor", pd.DataFrame({"日期": [int(month+"02")]*2,
                        "代码": ["SH600000", "SZ000001"], "is_st": [0, 0]})),
                ):
                    path = root / dataset / month[:4] / month[4:] / (month+".parquet")
                    path.parent.mkdir(parents=True)
                    frame.to_parquet(path, index=False)
            reader = AdjustedLimitProvider(root)
            market = pd.DataFrame({"code": ["SH600000", "SZ000001"], "adjusted_open": [22.12, 18.10],
                                   "previous_close": [20.10, 20.10], "is_suspended": False, "is_missing": False})
            original = market.copy(deep=True)
            with patch("pandas.read_parquet", wraps=pd.read_parquet) as read:
                for day in ("2024-01-02", "2024-01-02", "2024-02-02"):
                    result = reader.apply(day, market)
                    self.assertEqual(result.upper_limit.tolist(), [22.12, 22.12])
                    self.assertEqual(result.lower_limit.tolist(), [18.10, 18.10])
                    self.assertNotIn("raw_open", result)
                self.assertEqual(read.call_count, 4)
            pd.testing.assert_frame_equal(market, original)
            missing_reference = reader.apply("2024-02-02", market.assign(previous_close=None))
            self.assertTrue(missing_reference.upper_limit.isna().all())
            with self.assertRaisesRegex(ValueError, "MarketDataRawOpen"):
                reader.apply("2024-02-05", market)
            with self.assertRaises(FileNotFoundError):
                reader.apply("2024-03-02", market)

    def test_missing_invalid_raw_open_and_duplicate_keys_fail(self):
        for values in ([None], [0.0], [float("inf")], [11.0, 11.0]):
            with self.subTest(values=values), TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "MarketDataRawOpen/2024/01/202401.parquet"
                path.parent.mkdir(parents=True)
                pd.DataFrame({"日期":20240102,"代码":"SH600000","open":values}).to_parquet(path,index=False)
                path = root / "Factor33_winsor/2024/01/202401.parquet"
                path.parent.mkdir(parents=True)
                pd.DataFrame({"日期":[20240102],"代码":["SH600000"],"is_st":[0]}).to_parquet(path,index=False)
                market = pd.DataFrame([dict(code="SH600000",adjusted_open=22.0,previous_close=20.0,
                                           is_suspended=False,is_missing=False)])
                with self.assertRaisesRegex(ValueError, "duplicate|MarketDataRawOpen"):
                    AdjustedLimitProvider(root).apply("2024-01-02",market)
