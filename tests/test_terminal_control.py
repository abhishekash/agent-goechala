import io
import json
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from textual.widgets import Button, DataTable, TextArea

from observatory.terminal import Api
from observatory.terminal_ui import ObservatoryApp
from tests.test_terminal_search import ManualFeed


class ControlApi:
    endpoint = 'http://example.invalid'

    def __init__(self, *, questions=False):
        self.posts = []
        self.snapshot = {
            'sessions': [{'runtime': 'Codex', 'session_id': 'thread-1', 'state': 'waiting',
                          'project': 'observatory', 'goal': 'Fix the flaky test', 'updated_ns': 1,
                          'waiting_for': 'permission'}],
            'attention': [{'runtime': 'Codex', 'session_id': 'thread-1', 'id': 'request-1',
                           'kind': 'question' if questions else 'permission',
                           'detail': {'questions': [{'question': 'Which test?', 'options': [{'label': 'control'}]}]}
                           if questions else {'permission': 'shell', 'metadata': {'command': 'pytest'}}}]}

    def get(self, path):
        route = urlsplit(path)
        if route.path == '/api/control':
            return self.snapshot
        if route.path == '/api/control/session':
            session_id = parse_qs(route.query)['id'][0]
            return {'session_id': session_id, 'goal': 'Fix the flaky test',
                    'progress_summary': 'Goal: Fix the flaky test\nCurrent: Waiting for permission\nLatest: pytest',
                    'events': [{'domain': 'prompt', 'action': 'started', 'detail': 'Run the tests'}]}
        raise AssertionError(path)

    def post(self, path, body):
        self.posts.append((path, body))
        if path == '/api/control/summary':
            return {'summary': 'Done: test run\nCurrent: Reviewing failures\nRemaining: Confirm result'}
        return {'accepted': True}


class TerminalControlTests(unittest.IsolatedAsyncioTestCase):
    async def launch_app(self, *, questions=False):
        feed = ManualFeed()
        api = ControlApi(questions=questions)
        feed.api = api
        app = ObservatoryApp(feed)
        return app, api

    async def wait_until(self, pilot, predicate):
        for _ in range(20):
            if predicate():
                return
            await pilot.pause(0.1)
        self.fail('Timed out waiting for the terminal control view.')

    async def test_control_tab_lists_sessions_and_approves_in_place(self):
        app, api = await self.launch_app()
        async with app.run_test(size=(140, 42)) as pilot:
            await self.wait_until(pilot, lambda: app.query_one('#control-requests', DataTable).row_count == 1
                                  and app.query_one('#control-approve-once').display
                                  and app.query_one('#control-approve-once').region.width > 0)
            self.assertEqual(app.query_one('#pages').active, 'control')
            self.assertEqual(app.query_one('#control-sessions', DataTable).row_count, 1)
            self.assertEqual(app.query_one('#control-requests', DataTable).row_count, 1)
            self.assertTrue(app.query_one('#control-alert').display)
            self.assertTrue(app.query_one('#control-approve-once').display)
            await pilot.click('#control-approve-once')
            await self.wait_until(pilot, lambda: any(path == '/api/control/reply' for path, _ in api.posts))
            reply = next(body for path, body in api.posts if path == '/api/control/reply')
            self.assertEqual(reply['decision'], 'accept')

    async def test_control_tab_sends_answer_to_agent_question(self):
        app, api = await self.launch_app(questions=True)
        async with app.run_test(size=(140, 42)) as pilot:
            answer = app.query_one('#control-answer', TextArea)
            await self.wait_until(pilot, lambda: app.query_one('#control-requests', DataTable).row_count == 1
                                  and answer.display and app.query_one('#control-answer-send').region.width > 0)
            self.assertTrue(answer.display)
            answer.text = 'control'
            app.send_control_reply(answer=True)
            await self.wait_until(pilot, lambda: any(path == '/api/control/reply' for path, _ in api.posts))
            reply = next(body for path, body in api.posts if path == '/api/control/reply')
            self.assertEqual(reply['answers'], [['control']])

    async def test_control_tab_is_monitor_only(self):
        app, api = await self.launch_app()
        api.snapshot['attention'] = []
        api.snapshot['sessions'] = []
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            self.assertEqual(list(app.query('#control-start')), [])
            self.assertEqual(list(app.query('#control-launch')), [])
            self.assertEqual(api.posts, [])

    async def test_control_tab_loads_goal_and_progress_for_selected_session(self):
        app, api = await self.launch_app()
        api.snapshot['attention'] = []
        async with app.run_test(size=(140, 42)) as pilot:
            await self.wait_until(pilot, lambda: getattr(app,'control_session_detail',None) is not None)
            self.assertEqual(app.control_session_detail['session_id'], 'thread-1')
            self.assertTrue(app.query_one('#control-summary').display)
            self.assertFalse(app.query_one('#control-requests').display)
            self.assertFalse(app.query_one('#control-alert').display)
            self.assertFalse(app.query_one('#control-alert').has_class('pulse'))
            self.assertIn('Current: Waiting for permission', app.control_session_detail['progress_summary'])
            self.assertGreaterEqual(app.query_one('#control-detail').region.height, 14)
            await self.wait_until(pilot, lambda: any(path == '/api/control/summary' for path, _ in api.posts))
            self.assertIn(('/api/control/summary', {'runtime': 'Codex', 'session_id': 'thread-1'}), api.posts)
            await self.wait_until(pilot, lambda: app.control_generated_summary is not None)
            self.assertIn('Done: test run', app.control_generated_summary[1])
            self.assertEqual(str(app.query_one('#control-summary', Button).label), 'Refresh progress summary')


class TerminalApiTests(unittest.TestCase):
    def test_post_sends_json_and_local_authentication(self):
        api = Api('http://127.0.0.1:14318', 'secret')
        with patch('observatory.terminal.urllib.request.urlopen', return_value=io.BytesIO(b'{"accepted":true}')) as open_url:
            self.assertEqual(api.post('/api/control/reply', {'decision': 'accept'}), {'accepted': True})
        request = open_url.call_args.args[0]
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(request.get_header('Authorization'), 'Bearer secret')
        self.assertEqual(json.loads(request.data), {'decision': 'accept'})


if __name__ == '__main__':
    unittest.main()
