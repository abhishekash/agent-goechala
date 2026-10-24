import contextlib
import io
import unittest
from types import SimpleNamespace

from observatory.terminal import Snapshot, print_snapshot


class SnapshotUnknownUsageTests(unittest.TestCase):
    def test_unknown_usage_is_not_zero_or_a_formatting_crash(self):
        snapshot = Snapshot(totals={'runs': 1, 'spans': 1, 'input_tokens': None,
                                    'output_tokens': None, 'known_cost_usd': None,
                                    'usage_complete': False})
        feed = SimpleNamespace(refresh=lambda: None, snapshot=snapshot,
                               api=SimpleNamespace(endpoint='http://localhost:14318'))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_snapshot(feed)
        text = output.getvalue()
        self.assertIn('Tokens Not reported in / Not reported out', text)
        self.assertIn('Known cost Not reported', text)
