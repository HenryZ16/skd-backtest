"""Memory measurements must account for uneven intervals and stop on failures."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from examples.benchmark_data_flow import MemorySampler


class MemorySamplerTest(unittest.TestCase):
    def test_time_weighted_mean_and_sampled_peak(self):
        with (patch("examples.benchmark_data_flow.Thread"),
              patch("examples.benchmark_data_flow.psutil.Process") as process,
              patch("examples.benchmark_data_flow.perf_counter", side_effect=[10., 11., 14.])):
            process.return_value.memory_info.side_effect = [
                SimpleNamespace(rss=value) for value in (100, 300, 500)
            ]
            with MemorySampler(0.01) as sampler:
                sampler._sample()
            # Trapezoids: (100+300)/2*1 + (300+500)/2*3 = 1400 byte-seconds.
            self.assertEqual(sampler.stats, {
                "peak_rss_bytes": 500, "mean_rss_bytes": 350., "sample_count": 3,
                "duration_seconds": 4., "max_sample_gap_seconds": 3.,
            })
            self.assertTrue(sampler.stop.is_set())
            sampler.thread.join.assert_called_once()

    def test_sampler_and_workload_errors_stop_the_thread(self):
        sampler = MemorySampler(0.001)
        with (patch.object(sampler.process, "memory_info", side_effect=[
                  SimpleNamespace(rss=100), OSError("memory unavailable"),
              ]), self.assertRaisesRegex(OSError, "memory unavailable")):
            with sampler:
                self.assertTrue(sampler.stop.wait(2))
        self.assertFalse(sampler.thread.is_alive())

        sampler = MemorySampler(0.001)
        with self.assertRaisesRegex(RuntimeError, "workload failed"):
            with sampler:
                raise RuntimeError("workload failed")
        self.assertFalse(sampler.thread.is_alive())
        for interval in (0, -1, float("inf"), float("nan")):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                MemorySampler(interval)


if __name__ == "__main__":
    unittest.main()
