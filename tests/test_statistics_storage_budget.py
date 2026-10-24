import json
import unittest

from observatory.storage_derived import _compact_statistics


class StatisticsStorageBudgetTests(unittest.TestCase):
    def test_nonusage_span_does_not_persist_all_zero_fields(self):
        encoded = _compact_statistics({'mask': 0, 'input_tokens': 0, 'cost': None,
                                       'complete': False, 'unsupported': False,
                                       'model': None, 'error': False, 'active': False})
        self.assertEqual(json.loads(encoded), {'mask': 0})
        self.assertLessEqual(len(encoded), 16)

    def test_nonzero_values_and_truthy_flags_are_preserved(self):
        self.assertEqual(json.loads(_compact_statistics({'mask': 1, 'input_tokens': 12,
                         'error': True, 'model': 'model-a'})),
                         {'mask': 1, 'input_tokens': 12, 'error': True, 'model': 'model-a'})
