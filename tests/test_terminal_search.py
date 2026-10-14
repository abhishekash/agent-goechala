"""Regression coverage for filters that used to hide unrelated MCP traffic."""
import json
import time
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from observatory.terminal import Feed
from observatory.terminal_ui import ObservatoryApp


class ExampleApi:
    endpoint = 'http://example.invalid'

    def __init__(self):
        now = time.time_ns()
        self.activity = [{'source_id': 'event-1', 'service': 'codex', 'session_id': 'session-1',
                          'time_unix_nano': now, 'domain': 'shell', 'action': 'run',
                          'subject': 'git status --short', 'result': 'success', 'detail': ''}]
        self.mcp = [{'id': 'call-1', 'service': 'codex', 'session_id': 'session-1',
                     'time_unix_nano': now, 'server': 'github', 'tool': 'search_issues',
                     'method': 'tools/call', 'status': 'OK', 'duration_ms': 42,
                     'arguments': {'query': 'migration'}, 'output': {'matches': 2},
                     'source': 'span', 'trace_id': 'trace-1', 'span_id': 'span-1'}]
        self.traces = [{'trace_id': 'trace-1', 'task': 'Review migration', 'status': 'OK',
                        'span_count': 1, 'input_tokens': 100, 'output_tokens': 20, 'services': ['codex']}]
        self.sessions = [
            {'source_id': 'source-1', 'session_id': 'session-1', 'service': 'codex', 'runtime': 'Codex',
             'task': 'Review migration', 'project': 'payments', 'model': 'model-a', 'last_activity': self.activity[0]},
            {'source_id': 'source-2', 'session_id': 'session-2', 'service': 'claude', 'runtime': 'Claude',
             'task': 'Write release notes', 'project': 'docs', 'model': 'model-b'}]
        self.connections = [{'source_id': item['source_id'], 'service': item['service'],
                             'integrations': ['local-session-log'], 'span_count': 1} for item in self.sessions]

    def get(self, path):
        route = urlsplit(path)
        query = parse_qs(route.query).get('q', [''])[0].casefold()
        def matches(items):
            return [item for item in items if query in json.dumps(item).casefold()]
        if route.path == '/api/health':
            return {'local_agents': {'sessions': self.sessions, 'runtime': {'processes': []}}}
        if route.path == '/api/metrics':
            return {'input_tokens': 100, 'output_tokens': 20}
        if route.path == '/api/connections':
            return {'connections': self.connections}
        if route.path == '/api/activity':
            return {'activity': matches(self.activity)}
        if route.path == '/api/mcp':
            return {'calls': matches(self.mcp)}
        if route.path == '/api/sessions':
            return {'sessions': []}
        if route.path == '/api/traces':
            return {'traces': matches(self.traces)}
        if route.path.startswith('/api/traces/'):
            return {'trace_id': 'trace-1', 'summary': self.traces[0], 'spans': []}
        if route.path == '/api/findings':
            return {'findings': []}
        raise AssertionError(path)


class ManualFeed(Feed):
    """Refresh synchronously so UI checks never use a socket or a polling thread."""
    def __init__(self):
        super().__init__(ExampleApi())
        self.thread = SimpleNamespace(is_alive=lambda: True)
        self.refresh()

    def request_refresh(self):
        self.refresh()


class FeedSearchTest(unittest.TestCase):
    def test_searches_preserve_inventory_and_other_views(self):
        feed = ManualFeed()
        feed.query = 'release'
        feed.activity_query = 'git status'
        feed.mcp_query = 'missing-server'
        feed.refresh()
        self.assertEqual(feed.snapshot.traces, [])
        self.assertEqual(len(feed.snapshot.activity_results), 1)
        self.assertEqual(feed.snapshot.mcp_results, [])
        self.assertEqual(len(feed.snapshot.mcp), 1)
        self.assertEqual(len(feed.snapshot.activity), 1)
        self.assertEqual(feed.snapshot.queries, {'activity': 'git status', 'mcp': 'missing-server', 'traces': 'release'})


class CockpitSearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_filters_stay_with_results_and_do_not_leak_between_tabs(self):
        feed = ManualFeed()
        app = ObservatoryApp(feed)
        async with app.run_test(size=(150, 42)) as pilot:
            await pilot.pause()
            app.query_one('#search-agents').value = 'release'
            await pilot.pause()
            self.assertEqual(app.query_one('#agent-table').row_count, 1)
            self.assertEqual(app.selected_source, 'source-2')
            self.assertEqual(feed.query, '')
            self.assertEqual(app.query_one('#mcp-table').row_count, 1)
            self.assertEqual(len(feed.snapshot.activity), 1)

            app.action_page('mcp')
            app.action_search()
            await pilot.pause()
            self.assertEqual(app.focused.id, 'search-mcp')
            self.assertEqual(app.query_one('#search-mcp').value, '')
            field = app.query_one('#search-mcp')
            table = app.query_one('#mcp-table')
            self.assertEqual(field.parent.parent, table.parent)
            self.assertLessEqual(table.region.y - field.region.bottom, 1)
            field.value = 'missing-server'
            await pilot.pause(.4)
            self.assertEqual(table.row_count, 0)
            self.assertEqual(len(feed.snapshot.mcp), 1)
            self.assertIn('No matching', str(app.query_one('#mcp-hint').render()))

            await pilot.click('#clear-mcp')
            await pilot.pause(.4)
            self.assertEqual(table.row_count, 1)
            self.assertEqual(field.value, '')
            self.assertEqual(app.query_one('#search-agents').value, 'release')

            await pilot.resize_terminal(90, 32)
            await pilot.pause()
            self.assertGreater(table.region.height, 0)
            self.assertLessEqual(table.region.y - field.region.bottom, 1)
            app.action_page('agents')
            await pilot.pause()
            self.assertEqual(app.query_one('#agent-table').row_count, 1)
            await pilot.click('#clear-agents')
            await pilot.pause()
            self.assertEqual(app.query_one('#agent-table').row_count, 2)
            await pilot.press('escape')
            self.assertEqual(app.focused.id, 'agent-table')
        # A refresh tick queued before shutdown must tolerate removed widgets.
        app.refresh_data()
