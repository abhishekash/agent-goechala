import unittest

from observatory.ai_summary import summary_evidence


class SummaryCoverageTests(unittest.TestCase):
    def test_partial_status_cannot_be_summarized_as_zero_errors(self):
        evidence = summary_evidence({'telemetry_totals': {
            'status_complete': False, 'usage_complete': False, 'errors': 0,
            'failed': 0, 'incomplete': 0, 'input_tokens': None,
            'trace_count': 2, 'span_count': 1000, 'private': 'not-exported'}}, [])
        for key in ('errors', 'failed_runs', 'incomplete_runs', 'input_tokens'):
            self.assertIsNone(evidence['observed_totals'][key])
        self.assertEqual(evidence['observed_totals']['spans'], 1000)
        self.assertEqual(evidence['analysis_coverage'], {
            'usage_complete': False, 'status_complete': False})
