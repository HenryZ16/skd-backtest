"""Friendly output must preserve results, quiet mode, and failure handling."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch

import pandas as pd

from skd_backtest.console import ConsoleReporter
from skd_backtest.evaluate import main
from skd_backtest.schemas import RESULT_COLUMNS
import test_financial_integration as fixtures


class ConsoleTest(unittest.TestCase):
    setUp = fixtures.FinancialIntegrationTest.setUp
    engine = fixtures.FinancialIntegrationTest.engine
    inference = fixtures.FinancialIntegrationTest.inference

    def test_default_output_and_quiet_mode_have_identical_financial_results(self):
        engine = self.engine(output_dir=self.root / "friendly")
        with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as progress:
            metrics = engine.run()
        self.assertTrue(engine.config.friendly_output)
        self.assertIn("回测完成 | 4 个交易日", output.getvalue())
        self.assertIn("32.00%", output.getvalue())
        self.assertIn("300.00%", output.getvalue())
        self.assertIn("N/A", output.getvalue())
        self.assertIn(str(engine.config.output_dir), output.getvalue())
        self.assertEqual(sum(line.startswith("| ") for line in output.getvalue().splitlines()), 16)
        self.assertIn("0/4 交易日", progress.getvalue())
        self.assertIn("100.0% | 4/4 交易日 | 2018-01-05", progress.getvalue())
        self.assertNotIn("\r", progress.getvalue())

        quiet = self.engine(friendly_output=False, output_dir=self.root / "quiet")
        with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as progress:
            self.assertEqual(quiet.run(), metrics)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(progress.getvalue(), "")
        self.assertEqual(quiet.account, engine.account)
        for name in RESULT_COLUMNS:
            pd.testing.assert_frame_equal(quiet.tables[name], engine.tables[name])
        for name in ["metrics.json", *(name + ".csv" for name in RESULT_COLUMNS)]:
            self.assertEqual((quiet.config.output_dir / name).read_bytes(),
                             (engine.config.output_dir / name).read_bytes())

    def test_empty_single_day_and_repeated_runs(self):
        for date, days in (("2018-01-06", 0), ("2018-01-02", 1)):
            engine = self.engine(start_date=date, end_date=date, async_inference=False)
            for _ in range(2):
                with (self.subTest(date=date), redirect_stdout(StringIO()) as output,
                      redirect_stderr(StringIO()) as progress):
                    engine.run()
                self.assertIn(f"回测完成 | {days} 个交易日", output.getvalue())
                self.assertIn("区间内无交易日" if days == 0 else "100.0% | 1/1", progress.getvalue())

    def test_failures_end_terminal_progress_without_a_success_table(self):
        for failure in ("inference", "cleanup"):
            engine = self.engine()
            if failure == "inference":
                target = patch.object(engine.submission_runner, "inference", side_effect=RuntimeError("model failed"))
            else:
                close = engine.result_writer.close

                def fail_close(*, cache):
                    close(cache=cache)
                    raise RuntimeError("cleanup failed")

                target = patch.object(engine.result_writer, "close", fail_close)
            with (self.subTest(failure=failure), redirect_stdout(StringIO()) as output,
                  redirect_stderr(StringIO()) as progress, patch.object(progress, "isatty", return_value=True),
                  target, self.assertRaisesRegex(RuntimeError, "failed")):
                engine.run()
            self.assertEqual(output.getvalue(), "")
            self.assertIn("\r", progress.getvalue())
            self.assertTrue(progress.getvalue().endswith("\n"))
            self.assertIsNone(engine.metrics)
            self.assertIsNone(engine.data_provider._executor)

    def test_progress_throttles_updates_and_always_shows_the_last_day(self):
        for terminal, interval in ((True, 0.2), (False, 5.0)):
            with (self.subTest(terminal=terminal), redirect_stderr(StringIO()) as output,
                  patch.object(output, "isatty", return_value=terminal),
                  patch("skd_backtest.console.perf_counter", return_value=10.0) as clock):
                reporter = ConsoleReporter(True)
                reporter.progress(0, 100)
                reporter.progress(1, 100, "2018-01-02")
                clock.return_value += interval / 2
                reporter.progress(2, 100, "2018-01-03")
                clock.return_value += interval
                reporter.progress(70, 100, "2018-04-12")
                reporter.progress(100, 100, "2018-05-25")
                reporter.close()
            self.assertNotIn("2/100", output.getvalue())
            for count in (0, 1, 70, 100):
                self.assertIn(f"{count}/100 交易日", output.getvalue())
            self.assertIn("剩余约", output.getvalue())
            self.assertTrue(output.getvalue().endswith("\n"))

    def test_output_setting_requires_a_boolean(self):
        for value in ("false", 0, None):
            with self.subTest(value=value), self.assertRaisesRegex(TypeError, "friendly_output"):
                self.engine(friendly_output=value)



class EvaluationHelpTest(unittest.TestCase):
    def test_help_explains_usage_and_contains_the_complete_standalone_example(self):
        example = (Path(__file__).resolve().parents[1] / "examples" / "basic_usage.py").read_text(encoding="utf-8").strip()
        for flag in ("-h", "-help", "--help"):
            output = StringIO()
            with self.subTest(flag=flag), redirect_stdout(output), self.assertRaises(SystemExit) as exited:
                main([flag])
            self.assertEqual(exited.exception.code, 0)
            help_text = output.getvalue()
            self.assertTrue(help_text.startswith("usage: skd-backtest-evaluate "))
            self.assertIn("推荐用于评测平台批量回测时使用", help_text)
            self.assertIn("每次调用处理一个标准模型提交", help_text)
            self.assertIn("用户对自己的单个模型进行回测时，建议参考 examples/basic_usage.py", help_text)
            api_paragraph = next(p for p in help_text.split("\n\n") if "用户对自己的单个模型" in p)
            cli_paragraph = next(p for p in help_text.split("\n\n") if "skd-backtest-evaluate 是本包" in p)
            self.assertNotEqual(api_paragraph, cli_paragraph)
            self.assertTrue(api_paragraph.startswith("Python API（个人单模型回测）："))
            self.assertTrue(cli_paragraph.startswith("命令行评测（评测平台批量回测）："))
            self.assertIn(
                "\n".join(line for line in example.splitlines() if line.strip()),
                "\n".join(line for line in help_text.splitlines() if line.strip()),
            )


if __name__ == "__main__":
    unittest.main()
