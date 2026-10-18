import io
import json
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from textual.widgets import DataTable, Select, TextArea

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
                           if questions else {'permission': 'shell', 'metadata': {'command': 'pytest'}}}],
            'available_runtimes': {'OpenCode': False, 'Codex': True, 'Claude Code': False},
            'launches': []}

    def get(self, path):
        route = urlsplit(path)
        if route.path == '/api/control':
            return self.snapshot
        if route.path == '/api/control/session':
            session_id = parse_qs(route.query)['id'][0]
            return {'session_id': session_id, 'goal': 'Fix the flaky test',
                    'events': [{'domain': 'prompt', 'action': 'started', 'detail': 'Run the tests'}]}
        raise AssertionError(path)

    def post(self, path, body):
        self.posts.append((path, body))
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
            await self.wait_until(pilot, lambda: bool(api.posts))
            self.assertEqual(api.posts[0][0], '/api/control/reply')
            self.assertEqual(api.posts[0][1]['decision'], 'accept')

    async def test_control_tab_sends_answer_to_agent_question(self):
        app, api = await self.launch_app(questions=True)
        async with app.run_test(size=(140, 42)) as pilot:
            answer = app.query_one('#control-answer', TextArea)
            await self.wait_until(pilot, lambda: app.query_one('#control-requests', DataTable).row_count == 1
                                  and answer.display and app.query_one('#control-answer-send').region.width > 0)
            self.assertTrue(answer.display)
            answer.text = 'control'
            app.send_control_reply(answer=True)
            await self.wait_until(pilot, lambda: bool(api.posts))
            self.assertEqual(api.posts[0][1]['answers'], [['control']])

    async def test_control_tab_starts_agent_from_first_prompt(self):
        app, api = await self.launch_app()
        api.snapshot['attention'] = []
        api.snapshot['sessions'] = []
        async with app.run_test(size=(140, 42)) as pilot:
            await self.wait_until(pilot, lambda: app.query_one('#control-runtime', Select).value == 'Codex')
            app.query_one('#control-runtime', Select).value = 'Codex'
            app.query_one('#control-prompt', TextArea).text = 'Find and fix the flaky test.'
            await pilot.click('#control-start')
            await self.wait_until(pilot, lambda: bool(api.posts))
            self.assertEqual(api.posts[0][0], '/api/control/launch')
            self.assertEqual(api.posts[0][1], {'runtime': 'Codex', 'prompt': 'Find and fix the flaky test.'})

    async def test_control_tab_loads_goal_and_progress_for_selected_session(self):
        app, api = await self.launch_app()
        api.snapshot['attention'] = []
        async with app.run_test(size=(140, 42)) as pilot:
            await self.wait_until(pilot, lambda: getattr(app,'control_session_detail',None) is not None)
            self.assertEqual(app.control_session_detail['session_id'], 'thread-1')
            self.assertTrue(app.query_one('#control-summary').display)


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
