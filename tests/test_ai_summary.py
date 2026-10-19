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

    def test_existing_openai_key_needs_no_observatory_provider_configuration(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'existing-agent-key'}, clear=True):
            config = SummaryConfig.from_env()
        self.assertEqual(config.endpoint, 'https://api.openai.com/v1/chat/completions')
        self.assertEqual(config.model, 'gpt-4.1-mini')
        self.assertEqual(config.key, 'existing-agent-key')
        self.assertNotIn('existing-agent-key', repr(config))

    def test_openai_compatible_base_url_and_model_are_reused(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'existing-agent-key',
                                       'OPENAI_BASE_URL': 'https://llm.example/v1',
                                       'OPENAI_MODEL': 'local-choice'}, clear=True):
            config = SummaryConfig.from_env()
        self.assertEqual(config.endpoint, 'https://llm.example/v1/chat/completions')
        self.assertEqual(config.model, 'local-choice')

    def test_evidence_is_scoped_bounded_and_omits_raw_payloads(self):
        events = [{'session_id': 'selected', 'service': 'codex', 'subject': 'read README', 'detail': 'private body'} for _ in range(40)]
        events.insert(0, {'session_id': 'other', 'subject': 'another session'})
        evidence = summary_evidence({'session_id': 'selected', 'task': 'Inspect files', 'api_key': 'secret',
            'telemetry_totals': {'trace_count': 4, 'span_count': 19, 'errors': 1, 'failed': 0,
                'incomplete': 1, 'input_tokens': 1200, 'output_tokens': 230,
                'known_cost_usd': 0.01234, 'unknown_cost_runs': 2, 'private_payload': 'must not send'}}, events)
        text = json.dumps(evidence)
        self.assertLessEqual(len(evidence['events']), 30)
        self.assertEqual(evidence['observed_totals'], {'runs': 4, 'spans': 19, 'errors': 1,
            'failed_runs': 0, 'incomplete_runs': 1, 'input_tokens': 1200, 'output_tokens': 230,
            'known_cost_usd': 0.01234, 'runs_with_unknown_cost': 2})
        for excluded in ('private body', 'another session', 'secret'):
            self.assertNotIn(excluded, text)
        self.assertNotIn('must not send', text)

    def test_unavailable_totals_are_not_fabricated_as_zero(self):
        evidence = summary_evidence({'session_id': 'session-1', 'spans': 4}, [])
        self.assertEqual(evidence['observed_totals'], {
            'runs': None, 'spans': 4, 'errors': None, 'failed_runs': None,
            'incomplete_runs': None, 'input_tokens': None, 'output_tokens': None,
            'known_cost_usd': None, 'runs_with_unknown_cost': None})

    def test_explicitly_unreported_token_fields_stay_null_in_ai_evidence(self):
        evidence = summary_evidence({'telemetry_totals': {
            'input_tokens': 0, 'input_tokens_reported': False,
            'output_tokens': 0, 'output_tokens_reported': False}}, [])
        self.assertIsNone(evidence['observed_totals']['input_tokens'])
        self.assertIsNone(evidence['observed_totals']['output_tokens'])

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
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 12)
        self.assertEqual(request.get_header('Authorization'), 'Bearer secret')
        opener.open.side_effect = HTTPError(config.endpoint, 401, 'private response', {}, None)
        with patch('observatory.ai_summary.build_opener', return_value=opener):
            with self.assertRaisesRegex(SummaryError, 'HTTP 401') as error:
                generate_summary(config, {})
        self.assertNotIn('private response', str(error.exception))
