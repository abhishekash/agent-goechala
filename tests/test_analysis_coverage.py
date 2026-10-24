import unittest

from observatory.ui_coverage import analysis_coverage


class AnalysisCoverageTests(unittest.TestCase):
    def test_partial_analysis_is_distinguished_from_unreported_or_zero(self):
        note = analysis_coverage({'usage_complete': False, 'status_complete': False,
                                  'search_terms_complete': False,
                                  'evidence_window': {'complete': False}})
        for expected in ('unknown totals are not zero', 'additional errors',
                         'not complete trace history', 'Activity history'):
            self.assertIn(expected, note)

    def test_complete_or_older_summary_does_not_claim_incompleteness(self):
        self.assertEqual(analysis_coverage({}), '')
        self.assertEqual(analysis_coverage({'usage_complete': True}), '')
