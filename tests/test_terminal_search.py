"""Regression coverage for filters that used to hide unrelated MCP traffic."""
import json
import shlex
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from observatory.terminal import Feed
from observatory.terminal_ui import (ObservatoryApp, copy_event_text, decode, decode_nested,
                                     headline, write_system_clipboard)


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
        self.spans = [{'trace_id': 'trace-1', 'span_id': 'span-1', 'parent_span_id': None,
                       'name': 'agent.run', 'status': 'OK', 'start_unix_nano': now-1_000_000,
                       'end_unix_nano': now, 'service': 'codex', 'attributes': {'agent.task': 'Review migration'}}]
        self.spans.append({**self.spans[0], 'span_id': 'span-2', 'parent_span_id': 'span-1', 'name': 'shell command'})
        self.traces[0]['span_count'] = 2
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
            return {'trace_id': 'trace-1', 'summary': self.traces[0], 'spans': self.spans}
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


class EvidenceRenderingTest(unittest.TestCase):
    def test_nested_json_text_is_unwrapped_and_multiline_diff_stays_formatted(self):
        app = ObservatoryApp(ManualFeed())
        diff = 'diff --git a/runner.py b/runner.py\n@@ -1 +1 @@\n-old()\n+new()'
        payload = {'type': 'input_text', 'text': diff}
        for _ in range(24):
            payload = {'wrapper': payload}
        payload = json.dumps(payload)

        decoded = decode(payload)
        for _ in range(24):
            decoded = decode(decoded['wrapper'])
        self.assertEqual(decoded['text'], diff)

        class Capture:
            def __init__(self):
                self.blocks = []
            def write(self, block):
                self.blocks.append(block)

        output = Capture()
        app.write_evidence(output, 'Recorded Content', payload)
        rendered = '\n'.join(getattr(block, 'plain', getattr(block, 'code', str(block)))
                              for block in output.blocks)
        self.assertTrue('diff --git a/runner.py b/runner.py\n@@ -1 +1 @@\n-old()\n+new()' in rendered)
        self.assertNotIn(r'\n@@', rendered)

    def test_json_encoded_string_in_text_block_is_pretty_printed(self):
        app = ObservatoryApp(ManualFeed())
        payload = {'chunk_id': 'run-12', 'exit_code': 0, 'output': 'first line\nsecond line'}
        encoded_string = json.dumps(json.dumps(payload))
        self.assertEqual(decode(encoded_string), payload)
        deeply_nested = {'type': 'input_text', 'text': encoded_string}
        for _ in range(20):
            deeply_nested = {'wrapper': deeply_nested}
        normalized = decode_nested(deeply_nested)
        for _ in range(20):
            normalized = normalized['wrapper']
        self.assertEqual(normalized['text'], payload)

        class Capture:
            blocks = []
            def write(self, block):
                self.blocks.append(block)

        output = Capture()
        app.write_evidence(output, 'Text', encoded_string, depth=1)
        json_block = next(block for block in output.blocks if hasattr(block, 'code'))
        self.assertIn('\n  "chunk_id": "run-12",', json_block.code)
        self.assertIn('"output": "[multiline text · 2 lines shown below]"', json_block.code)
        self.assertTrue(any(getattr(block, 'plain', '') == 'first line\nsecond line' for block in output.blocks))

    def test_recorded_input_text_with_embedded_json_uses_indented_json(self):
        app = ObservatoryApp(ManualFeed())
        payload = {'chunk_id': '2497a1', 'wall_time_seconds': 4.78, 'exit_code': 0,
                   'output': 'main CI\nRun actions/setup-node\nRun node --check'}
        recorded = json.dumps({'type': 'input_text', 'text': json.dumps(payload)})

        class Capture:
            def __init__(self):
                self.blocks = []
            def write(self, block):
                self.blocks.append(block)

        output = Capture()
        app.write_evidence(output, 'Recorded Content', recorded)
        json_block = next(block for block in output.blocks if hasattr(block, 'code'))
        self.assertIn('\n  "chunk_id": "2497a1",', json_block.code)
        self.assertIn('"output": "[multiline text · 3 lines shown below]"', json_block.code)
        self.assertTrue(any(getattr(block, 'plain', '').startswith('main CI\nRun actions/setup-node') for block in output.blocks))
        self.assertNotIn(r'\"chunk_id\"', json_block.code)

    def test_shell_wrapped_python_heredoc_shows_readable_source(self):
        app = ObservatoryApp(ManualFeed())
        source = "import json\nprint(json.dumps({'status': 'ok'}))"
        command = "python - <<'PY'\n" + source + "\nPY"
        captured = '/usr/bin/zsh -lc ' + shlex.quote(command)

        class Capture:
            def __init__(self):
                self.blocks = []
            def write(self, block):
                self.blocks.append(block)

        output = Capture()
        app.write_evidence(output, 'Command', captured)
        code_block = next(block for block in output.blocks if hasattr(block, 'code'))
        self.assertEqual(code_block.code, source)
        self.assertEqual(code_block.lexer.name, 'Python')
        self.assertNotIn("/usr/bin/zsh", '\n'.join(getattr(block, 'plain', '') for block in output.blocks))

    def test_generic_exec_activity_row_uses_command_summary(self):
        command = "python - <<'PY'\nprint('ok')\nPY"
        item = {'domain': 'tool', 'action': 'execute_tool', 'subject': 'exec',
                'detail': json.dumps({'Command': '/usr/bin/zsh -lc ' + shlex.quote(command)})}
        self.assertEqual(headline(item), 'Python script')

    def test_multiline_json_values_are_shown_as_readable_text_blocks(self):
        app = ObservatoryApp(ManualFeed())
        output_text = 'Warning: truncated output\nTotal output lines: 3\nRun CI · Python 3.14'
        payload = {'output': output_text, 'exit_code': 0}

        class Capture:
            def __init__(self):
                self.blocks = []
            def write(self, block):
                self.blocks.append(block)

        output = Capture()
        app.write_evidence(output, 'Recorded Content', json.dumps(payload), depth=1)
        json_block = next(block for block in output.blocks if hasattr(block, 'code'))
        self.assertIn('"output": "[multiline text · 3 lines shown below]"', json_block.code)
        readable_text = next(block for block in output.blocks if getattr(block, 'plain', '') == output_text)
        self.assertIn('Total output lines: 3\nRun CI', readable_text.plain)
        self.assertNotIn(r'\nTotal output lines', readable_text.plain)

    def test_embedded_json_in_recorded_log_is_formatted(self):
        app = ObservatoryApp(ManualFeed())
        output_text = 'Warning: truncated output\nTotal output lines: 1'
        recorded = 'Script completed\nWall time 0.0 seconds\nOutput:\n{"output":' + json.dumps(output_text) + ',"session_id":6396}'

        class Capture:
            def __init__(self):
                self.blocks = []
            def write(self, block):
                self.blocks.append(block)

        output = Capture()
        app.write_evidence(output, 'Recorded Content', recorded)
        json_block = next(block for block in output.blocks if hasattr(block, 'code'))
        self.assertIn('\n  "output": "[multiline text · 2 lines shown below]"', json_block.code)
        self.assertTrue(any(getattr(block, 'plain', '') == output_text for block in output.blocks))

    def test_copy_event_text_expands_embedded_json(self):
        readable_output = 'Warning: truncated output\nTotal output lines: 1'
        item = {'domain': 'tool', 'action': 'wait', 'subject': 'wait', 'service': 'codex',
                'result': 'observed', 'time_unix_nano': time.time_ns(),
                'detail': '{"output":' + json.dumps(readable_output) + ',"session_id":6396}'}
        copied = copy_event_text(item)
        self.assertIn('"output": "[multiline text · 2 lines shown below]"', copied)
        self.assertIn(readable_output, copied)
        self.assertNotIn(r'\nTotal output lines', copied)

    def test_copy_mcp_call_includes_arguments_and_response(self):
        item = {'domain': 'mcp', 'server': 'github', 'tool': 'search_issues',
                'status': 'OK', 'arguments': {'query': 'formatting'},
                'output': {'matches': 2}}
        copied = copy_event_text(item)
        self.assertIn('github / search_issues', copied)
        self.assertIn('"query": "formatting"', copied)
        self.assertIn('"matches": 2', copied)

    def test_native_clipboard_uses_available_x11_backend(self):
        with patch('observatory.terminal_ui.os.environ', {'DISPLAY': ':0'}), \
             patch('observatory.terminal_ui.shutil.which', return_value='/usr/bin/xclip'), \
             patch('observatory.terminal_ui.subprocess.run') as run:
            run.return_value.returncode = 0
            self.assertEqual(write_system_clipboard('copy me'), 'X11')
        run.assert_called_once_with(['xclip', '-selection', 'clipboard', '-i'], input='copy me',
                                    text=True, capture_output=True, timeout=3, check=False)


class CockpitSearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_compact_inspection_resize_and_error_triage(self):
        feed = ManualFeed()
        app = ObservatoryApp(feed)
        async with app.run_test(size=(180, 50)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one('#span-tree').cursor_node.data['span_id'], 'span-1')
            self.assertTrue(app.query_one('#span-tree').cursor_node.is_expanded)
            self.assertEqual(app.inspector_signatures['span-info'][0], 'agent.run')
            agents = app.query_one('#agent-table')
            agents.move_cursor(row=1)
            await pilot.pause()
            self.assertEqual(app.selected_source, 'source-2')
            for width, height in ((120, 40), (90, 32), (80, 24), (180, 50)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                self.assertEqual(app.selected_source, 'source-2')
                self.assertEqual(agents.cursor_row, 1)
                for page in ('agents', 'activity', 'mcp', 'traces', 'host'):
                    app.action_page(page)
                    await pilot.pause()
                    table = app.query_one('#'+{'agents': 'agent', 'activity': 'activity', 'mcp': 'mcp', 'traces': 'trace', 'host': 'host'}[page]+'-table')
                    self.assertGreaterEqual(table.size.height, 3, (page, width, height))
                    self.assertLessEqual(table.virtual_size.width, table.scrollable_content_region.width, (page, width, height))

            app.action_page('mcp')
            await pilot.pause()
            await pilot.click('#errors-mcp')
            await pilot.pause()
            self.assertEqual(app.query_one('#mcp-table').row_count, 0)
            self.assertTrue(app.query_one('#mcp-trace').disabled)
            self.assertEqual(len(feed.snapshot.mcp), 1)
            self.assertIn('No failed MCP calls', app.inspector_signatures['mcp-detail'][0])
            app.action_all()
            await pilot.pause()
            self.assertEqual(app.query_one('#mcp-table').row_count, 1)
            self.assertFalse(app.query_one('#mcp-trace').disabled)

            await pilot.resize_terminal(80, 24)
            app.action_page('mcp')
            await pilot.pause()
            await pilot.press('enter')
            await pilot.pause()
            self.assertTrue(app.query_one('#mcp').has_class('expanded'))
            self.assertEqual(app.focused.id, 'mcp-detail')
            self.assertGreater(app.query_one('#mcp-detail').size.height, 8)
            self.assertLessEqual(app.query_one('#mcp-detail').virtual_size.width, app.query_one('#mcp-detail').scrollable_content_region.width)
            await pilot.press('escape')
            await pilot.pause()
            self.assertFalse(app.query_one('#mcp').has_class('expanded'))
            self.assertEqual(app.focused.id, 'mcp-table')

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
