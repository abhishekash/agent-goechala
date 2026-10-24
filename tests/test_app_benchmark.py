"""Keep resource reports descriptive and independent of personal session data."""
import unittest

from scripts.benchmark_app import percentile


class BenchmarkStatisticsTests(unittest.TestCase):
    def test_small_sample_percentiles_use_nearest_rank(self):
        self.assertEqual(percentile([5, 1, 3, 2, 4], .5), 3)
        self.assertEqual(percentile(list(range(1, 13)), .95), 12)
