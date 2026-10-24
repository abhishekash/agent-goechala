"""Regression coverage for filters that used to hide unrelated MCP traffic."""
import json
import shlex
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from observatory.terminal import Feed
from observatory.terminal_ui import (ObservatoryApp, ObservatoryScreen, SelectableRichLog,
                                     copy_event_text, decode, decode_nested, headline,
                                     usage_series_text, write_system_clipboard)


class ExampleApi:
    endpoint = 'http://example.invalid'

    def __init__(self):
        now = time.time_ns()
        self.requests = []
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
        self.mcp_configurations = []
        self.mcp_summary = {'calls': 1, 'tools': 1, 'errors': 0,
                            'servers': [{'server': 'github', 'calls': 1, 'tools': 1, 'errors': 0}]}
        self.findings = []
        self.metric_catalog = [{'service':'codex','name':'agent.queue.depth','description':'Pending work',
            'unit':'items','kind':'gauge','temporality':0,'scope_name':'demo.metrics',
            'point_count':2,'last_ns':now}]
        self.metric_points = [{'id':'point-1','time_unix_nano':now,'start_unix_nano':now-1_000_000_000,
            'service':'codex','name':'agent.queue.depth','description':'Pending work','unit':'items',
            'kind':'gauge','temporality':0,'scope_name':'demo.metrics','attributes':{'queue':'default'},
            'resource_attributes':{'service.name':'codex','deployment.environment':'test'},
            'data':{'value':3,'exemplars':[{'trace_id':'trace-1','span_id':'span-1','value':3}]}}]
        self.usage_series = [{'start_unix_nano':now,'input_tokens':120,'output_tokens':30,
            'input_tokens_reported':True,'output_tokens_reported':True,'runs':2,'errors':1,
            'cache_read_input_tokens':20,'cache_read_input_tokens_reported':True}]

    def get(self, path):
        self.requests.append(path)
        route = urlsplit(path)
        parameters = parse_qs(route.query)
        query = parameters.get('q', [''])[0].casefold()
        domain = parameters.get('domain', [''])[0].casefold()
        action = parameters.get('action', [''])[0].casefold()
        def matches(items):
            return [item for item in items if query in json.dumps(item).casefold()
                    and (not domain or str(item.get('domain') or '').casefold()==domain)
                    and (not action or str(item.get('action') or '').casefold()==action)]
        if route.path == '/api/health':
            return {'local_agents': {'sessions': self.sessions, 'runtime': {'processes': []},
                                     'mcp_configurations': self.mcp_configurations}}
        if route.path == '/api/metrics':
            return {'input_tokens': 100, 'output_tokens': 20}
        if route.path == '/api/connections':
            return {'connections': self.connections}
        if route.path == '/api/activity':
            service = parameters.get('service', [''])[0]
            session_id = parameters.get('session_id', [''])[0]
            return {'activity': [item for item in matches(self.activity)
                                 if (not service or item.get('service')==service)
                                 and (not session_id or item.get('session_id')==session_id)]}
        if route.path == '/api/mcp':
            return {'calls': matches(self.mcp), 'summary': self.mcp_summary}
        if route.path == '/api/sessions':
            return {'sessions': []}
        if route.path == '/api/findings':
            return {'findings': matches(self.findings), 'next_cursor': None}
        if route.path == '/api/metric-catalog':
            return {'metrics': self.metric_catalog}
        if route.path == '/api/measurements':
            return {'points': self.metric_points}
        if route.path == '/api/usage-series':
            return {'points': self.usage_series, 'service': parameters.get('service', [None])[0],
                    'session_id': parameters.get('session_id', [None])[0]}
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



class CockpitSearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_compact_inspection_resize_and_error_triage(self):
        feed = ManualFeed()
        app = ObservatoryApp(feed)
        async with app.run_test(size=(180, 50)) as pilot:
            await pilot.pause()
            # Trace inspectors are built on activation, not behind Agents.
            app.action_page('traces')
            await pilot.pause()
            self.assertEqual(app.query_one('#span-tree').cursor_node.data['span_id'], 'span-1')
            self.assertTrue(app.query_one('#span-tree').cursor_node.is_expanded)
            self.assertEqual(app.inspector_signatures['span-info'][0], 'agent.run')
            app.action_page('agents')
            await pilot.pause()
            agents = app.query_one('#agent-table')
            agents.move_cursor(row=1)
            await pilot.pause()
            self.assertEqual(app.selected_source, 'source-2')
            for width, height in ((120, 40), (90, 32), (80, 24), (180, 50)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                self.assertEqual(app.selected_source, 'source-2')
                self.assertEqual(agents.cursor_row, 1)
            app.action_page('activity')
            await pilot.pause()
            detail = app.query_one('#activity-detail')
            self.assertIsInstance(detail, SelectableRichLog)
            self.assertIsInstance(app.screen, ObservatoryScreen)
            detail.clear();detail.write('copy this text')
            await pilot.pause()
            # Textual routes mouse drags through Screen selection. Use absolute
            # screen coordinates as the terminal driver does, and verify the
            # RichLog contributes character offsets for the selected line.
            from textual.events import MouseDown, MouseMove, MouseUp
            start_x, start_y = detail.region.x + 2, detail.region.y
            for event_type, pointer_x in (
                (MouseDown, start_x),
                (MouseMove, start_x + 9),
                (MouseUp, start_x + 9),
            ):
                app.screen._forward_event(event_type(
                    widget=None, x=pointer_x, y=start_y,
                    delta_x=pointer_x - start_x, delta_y=0, button=1,
                    shift=False, meta=False, ctrl=False,
                    screen_x=pointer_x, screen_y=start_y,
                ))
                await pilot.pause()
            self.assertEqual(app.screen.get_selected_text(), 'py this te')
            with patch('observatory.terminal_ui.write_system_clipboard',return_value='X11'):
                app.screen.action_copy_text()
                for page in ('agents', 'activity', 'mcp', 'traces', 'host', 'metrics'):
                    app.action_page(page)
                    await pilot.pause()
                    table = app.query_one('#'+{'agents': 'agent', 'activity': 'activity', 'mcp': 'mcp', 'traces': 'trace', 'host': 'host', 'metrics':'metrics'}[page]+'-table')
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

            app.action_page('metrics')
            metric=app.table_data['metrics-table'][app.selected_metric]
            app.request_metric_history(metric,app.selected_metric)
            await app.workers.wait_for_complete()
            await pilot.pause()
            metrics=app.query_one('#metrics-table')
            self.assertEqual(metrics.row_count,1)
            self.assertEqual(str(metrics.get_row_at(0)[3]),'3')
            self.assertIn('agent.queue.depth',app.inspector_signatures['metrics-detail'][1])
            with patch('observatory.terminal_ui.write_system_clipboard',return_value='X11'):
                app.action_copy()
                await pilot.pause()
            self.assertEqual(json.loads(app._clipboard)['name'],'agent.queue.depth')
            self.assertIn('\n  "service": "codex",',app._clipboard)
            self.assertFalse(app.query_one('#metrics-trace').disabled)
            app.action_open_trace()
            await pilot.pause()
            self.assertEqual(feed.selected_trace,'trace-1')
            app.action_page('metrics')
            await pilot.pause()
            app.query_one('#search-metrics').value='missing'
            await pilot.pause()
            self.assertEqual(metrics.row_count,0)
            self.assertIn('No matching metric',str(app.query_one('#search-status-metrics').render()))
            app.query_one('#clear-metrics').press()
            await pilot.pause()
            self.assertEqual(metrics.row_count,1)

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
            self.assertEqual(len(feed.snapshot.activity), 1)

            app.action_page('mcp')
            app.action_search()
            await pilot.pause()
            self.assertEqual(app.query_one('#mcp-table').row_count, 1)
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


class FeedMetricsTest(unittest.TestCase):
    def test_feed_loads_metric_catalog_and_latest_points(self):
        feed=ManualFeed()
        self.assertTrue(feed.snapshot.metrics_available)
        self.assertEqual(feed.snapshot.metric_catalog[0]['name'],'agent.queue.depth')
        self.assertEqual(feed.snapshot.metric_points[0]['data']['value'],3)
        history=feed.metric_history(feed.snapshot.metric_catalog[0])
        self.assertEqual(history[0]['data']['value'],3)
        self.assertEqual(history[0]['data']['exemplars'][0]['trace_id'],'trace-1')
        latest_request=next(path for path in feed.api.requests if path.startswith('/api/measurements?'))
        latest_query=parse_qs(urlsplit(latest_request).query)
        self.assertEqual(latest_query.get('latest'),['1'])
        self.assertEqual(latest_query.get('limit'),['500'])

    def test_feed_loads_scoped_token_usage_series(self):
        feed=ManualFeed()
        self.assertEqual(feed.snapshot.usage_series,feed.api.usage_series)
        self.assertEqual(feed.snapshot.usage_service,None)
        request=next(path for path in feed.api.requests if path.startswith('/api/usage-series?'))
        query=parse_qs(urlsplit(request).query)
        self.assertEqual(query.get('days'),['7'])
        self.assertEqual(query.get('step'),['day'])

    def test_feed_requests_usage_for_the_selected_session(self):
        feed=ManualFeed()
        feed.session_id='session-1'
        feed.extra_cache=None
        feed.refresh()
        self.assertEqual(feed.snapshot.usage_session_id,'session-1')
        request=next(path for path in reversed(feed.api.requests) if path.startswith('/api/usage-series?'))
        self.assertEqual(parse_qs(urlsplit(request).query).get('session_id'),['session-1'])

    def test_usage_history_is_readable_and_marks_unreported_tokens(self):
        rendered=usage_series_text([{'start_unix_nano':time.time_ns(), 'input_tokens':120,
            'output_tokens':30,'input_tokens_reported':True,'output_tokens_reported':False,
            'runs':1,'errors':0}])
        self.assertIn('Input 120  ·  Output Not reported',rendered)
        self.assertIn('DATE       INPUT       OUTPUT      RUNS  ERRORS  LOGS',rendered)
        self.assertIn('       —',rendered)


class MetricDetailSemanticsTest(unittest.IsolatedAsyncioTestCase):
    async def test_summary_temporality_is_explained_in_metric_inspector(self):
        feed = ManualFeed()
        now = time.time_ns()
        feed.api.metric_catalog = [{
            'service': 'codex', 'name': 'agent.summary', 'description': 'Completed work',
            'unit': 'items', 'kind': 'summary', 'temporality': 0, 'scope_name': 'demo.metrics',
            'point_count': 1, 'last_ns': now,
        }]
        feed.api.metric_points = [{
            'id': 'summary-point', 'time_unix_nano': now, 'start_unix_nano': now - 1_000_000,
            'service': 'codex', 'name': 'agent.summary', 'description': 'Completed work',
            'unit': 'items', 'kind': 'summary', 'temporality': 0, 'scope_name': 'demo.metrics',
            'attributes': {'queue': 'default'}, 'resource_attributes': {'service.name': 'codex'},
            'data': {'count': 3, 'sum': 8.5, 'quantileValues': [{'quantile': 0.5, 'value': 2.5}],
                     'temporality_assumed': 'cumulative'},
        }]
        feed.extra_updated = 0
        feed.refresh()
        app = ObservatoryApp(feed)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_page('metrics')
            await pilot.pause()
            signature = app.inspector_signatures['metrics-detail']
            self.assertIn('agent.summary', signature[1])
            self.assertIn('count 3 · sum 8.5', signature[1])
            self.assertIn('Cumulative (implicit for Summary)', signature[1])
            self.assertIn("'temporality_assumed': 'cumulative'", signature[2])
            detail = app.query_one('#metrics-detail').plain_text
            self.assertIn('"quantile": 0.5', detail)
            self.assertIn('"value": 2.5', detail)
            self.assertIn('Temporality Assumed\ncumulative', detail)


class TokenPresenceRenderingTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_usage_is_labeled_not_reported_across_views(self):
        feed = ManualFeed()
        feed.snapshot.totals.update(input_tokens=0, output_tokens=0,
                                    input_tokens_reported=False, output_tokens_reported=False)
        feed.snapshot.traces[0].update(input_tokens=0, output_tokens=0,
                                       input_tokens_reported=False, output_tokens_reported=False)
        feed.snapshot.sessions = [{
            'session_id': 'session-1', 'trace_count': 1, 'span_count': 2,
            'input_tokens': 0, 'output_tokens': 0,
            'input_tokens_reported': False, 'output_tokens_reported': False,
            'known_cost_usd': 0.0, 'unknown_cost_runs': 1,
        }]
        app = ObservatoryApp(feed)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            self.assertIn('Not reported / Not reported', str(app.query_one('#stat-usage').render()))
            self.assertIn('Not reported in / Not reported out', app.query_one('#agent-detail').plain_text)
            app.action_page('traces')
            await pilot.pause()
            row = app.query_one('#trace-table').get_row_at(0)
            self.assertIn('Not reported / Not reported', str(row[3]))
