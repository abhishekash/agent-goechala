import io
import json
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError

from observatory.ai_summary import SummaryConfig, SummaryError, summary_evidence, generate_summary


class SummaryTests(unittest.TestCase):
    def test_explicit_configuration_and_key_not_in_repr(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(SummaryError):
                SummaryConfig.from_env()
        env = {'OBSERVATORY_SUMMARY_URL': 'https://example.invalid/v1/chat/completions',
               'OBSERVATORY_SUMMARY_MODEL': 'chosen-model', 'OBSERVATORY_SUMMARY_KEY_ENV': 'MY_KEY',
               'MY_KEY': 'secret-key'}
        with patch.dict('os.environ', env, clear=True):
            config = SummaryConfig.from_env()
            self.assertNotIn('secret-key', repr(config))
        with patch.dict('os.environ', {**env, 'OBSERVATORY_SUMMARY_URL': 'http://remote.invalid/v1'}, clear=True):
            with self.assertRaises(SummaryError): SummaryConfig.from_env()

    def test_evidence_is_scoped_bounded_and_omits_raw_payloads(self):
        events = [{'session_id': 'selected', 'service': 'codex', 'subject': 'read README', 'detail': 'private body'} for _ in range(40)]
        events.insert(0, {'session_id': 'other', 'subject': 'another session'})
        evidence = summary_evidence({'session_id': 'selected', 'task': 'Inspect files', 'api_key': 'secret'}, events)
        text = json.dumps(evidence)
        self.assertLessEqual(len(evidence['events']), 30)
        for excluded in ('private body', 'another session', 'secret'):
            self.assertNotIn(excluded, text)

    def test_request_contract_and_provider_errors_are_sanitized(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = io.BytesIO(json.dumps({
            'choices': [{'message': {'content': 'Read README. Awaiting input.'}}]}).encode())
        config = SummaryConfig('https://example.invalid/v1/chat/completions', 'chosen-model', 'secret')
        with patch('observatory.ai_summary.build_opener', return_value=opener):
            self.assertEqual(generate_summary(config, {'task': 'Inspect'}), 'Read README. Awaiting input.')
        request = opener.open.call_args.args[0]
        data = json.loads(request.data)
        self.assertEqual(data['model'], 'chosen-model')
        self.assertFalse(data['store'])
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 30)
        self.assertEqual(request.get_header('Authorization'), 'Bearer secret')
        opener.open.side_effect = HTTPError(config.endpoint, 401, 'private response', {}, None)
        with patch('observatory.ai_summary.build_opener', return_value=opener):
            with self.assertRaisesRegex(SummaryError, 'HTTP 401') as error:
                generate_summary(config, {})
        self.assertNotIn('private response', str(error.exception))
