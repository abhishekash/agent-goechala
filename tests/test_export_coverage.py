import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from observatory.ui_diagnostics import diagnostics_text
from observatory.ui_export import investigation_json, write_investigation


class InvestigationCoverageTests(unittest.TestCase):
    def test_partial_trace_export_keeps_page_and_coverage_metadata(self):
        record = {'trace_id': 'selected', 'spans': [{'span_id': 'one'}],
                  'span_page': {'trace_total': 10000, 'next_cursor': 'next'},
                  'summary': {'span_count': 10000}, 'token': 'must-not-export'}
        exported = json.loads(investigation_json('traces', record))
        self.assertIn('not a complete history', exported['coverage'])
        self.assertEqual(exported['evidence']['span_page'], record['span_page'])
        self.assertNotIn('must-not-export', json.dumps(exported))

    def test_diagnostic_paths_cannot_inject_terminal_or_bidi_controls(self):
        result = diagnostics_text({'sessions': [{'diagnostics': {
            'source_path': '/local/\x1b[31m\x9b\u202efile.jsonl'}}]})
        for control in ('\x1b', '\x9b', '\u202e'):
            self.assertNotIn(control, result)
        self.assertIn('/local/', result)

    def test_failed_write_does_not_leave_a_truncated_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'evidence.json'
            with patch('observatory.ui_export.os.fdopen', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    write_investigation(path, '{}\n')
            self.assertFalse(path.exists())
