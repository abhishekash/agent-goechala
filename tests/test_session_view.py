import time
import unittest
from unittest.mock import patch

from textual.widgets import DataTable, Input, Select
from observatory.terminal_ui import (ObservatoryApp, coverage_warning, input_required, recorded_progress,
                                     input_status, sources, wait_label, wait_summary)
from tests.test_terminal_search import ManualFeed


class SessionViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_tasks_get_spare_width_and_wrap_without_expanding_missing_rows(self):
        feed=ManualFeed()
        task=('Investigate the checkout regression and improve keyboard navigation '
              'for the payment form before preparing the next release.')
        feed.snapshot.local_status['sessions'][0]['task']=task
        feed.snapshot.local_status['sessions'][1]['task']='Task not reported'
        app=ObservatoryApp(feed)
        async with app.run_test(size=(160,48)) as pilot:
            for size in ((160,48),(120,40),(160,48)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                table=app.query_one('#agent-table',DataTable)
                self.assertNotIn('Waiting on',[str(column.label) for column in table.columns.values()])
                self.assertGreater(table.column_widths[-1],table.column_widths[2]*3)
                self.assertEqual(table.rows['source-1'].height,2)
                self.assertEqual(table.rows['source-2'].height,1)
                task_cell=table.get_row('source-1')[-1]
                self.assertFalse(task_cell.no_wrap)
                rendered='\n'.join(table.render_line(row).text for row in range(table.size.height))
                self.assertIn('Investigate the checkout',rendered)
                self.assertIn('keyboard navigation',rendered)
                self.assertIn(task,app.query_one('#agent-detail').plain_text.replace('\n',' '))
                self.assertIn('italic',str(table.get_row('source-2')[-1].style))
            # Live updates can shrink a wrapped row without changing row identity.
            feed.snapshot.local_status['sessions'][0]['task']='Fix checkout'
            app.last_update=-1
            app.refresh_data()
            await pilot.pause()
            self.assertEqual(table.rows['source-1'].height,1)
            self.assertEqual(table.get_row('source-1')[-1].plain,'Fix checkout')

    async def test_columns_waiting_and_summary_are_bound_to_selected_session(self):
        feed = ManualFeed()
        entry = feed.snapshot.local_status['sessions'][0]
        entry.update(run_state='waiting', waiting_for='User input', working_directory='/work/payments', branch='fix/mcp')
        feed.snapshot.sessions = [{'session_id': entry['session_id'], 'trace_count': 3,
            'span_count': 12, 'errors': 1, 'failed': 0, 'incomplete': 1,
            'input_tokens': 900, 'output_tokens': 240, 'known_cost_usd': 0.006,
            'unknown_cost_runs': 2}]
        with patch.dict('os.environ', {'OBSERVATORY_SUMMARY_URL': 'http://localhost:1234/v1/chat/completions',
                                       'OBSERVATORY_SUMMARY_MODEL': 'local-model'}):
            app = ObservatoryApp(feed)
            with patch('observatory.ai_summary.generate_summary', return_value='Reviewed migration. Waiting for your answer.') as generate:
                async with app.run_test(size=(160, 48)) as pilot:
                    await pilot.pause()
                    table = app.query_one('#agent-table', DataTable)
                    self.assertEqual([str(c.label) for c in table.columns.values()],
                        ['Agent', 'Session ID', 'Repo / project', 'Status', 'Updated', 'Task / details'])
                    row = ' '.join(str(value) for value in table.get_row_at(0))
                    self.assertIn('NEEDS REPLY', row)
                    self.assertNotIn('Reply · Codex terminal', row)
                    detail = app.query_one('#agent-detail').plain_text
                    self.assertIn('Progress Summary · Recorded Activity', detail)
                    self.assertIn('Current: Waiting', detail)
                    self.assertIn('answer in the Codex terminal', detail)
                    self.assertIn('Goechala is read-only', detail)
                    self.assertLess(detail.splitlines().index('Progress Summary · Recorded Activity'), 10)
                    # Mounting and refreshes never trigger paid API calls.
                    generate.assert_not_called()
                    await pilot.click('#agent-summary')
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertIn(entry['source_id'], app.ai_summaries)
                    self.assertEqual(generate.call_count, 1)
                    await pilot.click('#agent-summary')
                    await app.workers.wait_for_complete()
                    self.assertEqual(generate.call_count, 1)
                    self.assertEqual(generate.call_args.args[1]['project'], 'payments')
                    self.assertEqual(generate.call_args.args[1]['observed_totals'], {
                        'runs': 3, 'spans': 12, 'errors': 1, 'failed_runs': 0,
                        'incomplete_runs': 1, 'input_tokens': 900, 'output_tokens': 240,
                        'known_cost_usd': 0.006, 'runs_with_unknown_cost': 2})

    async def test_unconfigured_summary_is_available_locally_without_prompt_or_blank_panel(self):
        with patch.dict('os.environ', {'OBSERVATORY_SUMMARY_URL': '', 'OBSERVATORY_SUMMARY_MODEL': '',
                                       'OPENAI_API_KEY': '', 'OPENAI_BASE_URL': '', 'OPENAI_API_BASE': ''}):
            feed = ManualFeed()
            feed.api.activity.clear()
            feed.refresh()
            app = ObservatoryApp(feed)
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause()
                self.assertTrue(app.query_one('#agent-summary').disabled)
                self.assertIn('works without setup', app.query_one('#agent-summary').tooltip)
                detail = app.query_one('#agent-detail').plain_text
                self.assertIn('Progress Summary · Recorded Activity', detail)
                self.assertIn('Latest: git status --short', detail)
                self.assertNotIn('AI summary ·', detail)
                actions = app.query_one('#agent-actions', DataTable)
                self.assertEqual(actions.row_count, 1)
                self.assertIn('No recent activity captured', str(actions.get_row_at(0)))
                self.assertEqual(len(app.query('#agent-output')), 0)
                self.assertFalse(any(binding.key == 'b' for binding in app.BINDINGS))

    def test_wait_copy_and_reply_guidance_separate_user_input_from_tool_wait(self):
        reply = {'runtime': 'OpenCode', 'state': 'Waiting', 'waiting_for': 'User input'}
        self.assertTrue(input_required(reply))
        self.assertIn('OpenCode terminal', wait_label(reply))
        self.assertIn('read-only', wait_summary(reply))
        tool = {'runtime': 'Codex', 'state': 'Waiting', 'waiting_for': 'Tool response: github / search'}
        self.assertFalse(input_required(tool))
        self.assertIn('no user action', wait_summary(tool))

    async def test_sessions_needing_action_are_prioritized_and_labeled(self):
        feed = ManualFeed()
        now = time.time_ns()
        waiting = next(item for item in feed.snapshot.local_status['sessions']
                       if item['source_id'] == 'source-2')
        waiting.update(run_state='waiting', waiting_for='Approval',
                       last_activity={'time_unix_nano': now, 'domain': 'tool',
                                      'action': 'permission', 'subject': 'write file'})
        app = ObservatoryApp(feed)
        async with app.run_test(size=(150, 42)) as pilot:
            await pilot.pause()
            table = app.query_one('#agent-table', DataTable)
            self.assertEqual(next(iter(table.rows)).value, 'source-2')
            row = ' '.join(str(value) for value in table.get_row_at(0))
            self.assertIn('NEEDS APPROVAL', row)
            self.assertIn('Claude terminal', app.query_one('#agent-detail').plain_text)

    async def test_prompts_filter_shows_only_user_messages_for_scoped_agent(self):
        feed = ManualFeed()
        stamp = time.time_ns() + 1
        prompt = {'id':'prompt-1','source_id':'prompt-1','service':'codex','session_id':'session-1',
                  'time_unix_nano':stamp,'domain':'message','action':'user','subject':'Deploy the update',
                  'detail':'Deploy the update after the checks pass.','result':'observed'}
        assistant = {'id':'assistant-1','source_id':'assistant-1','service':'codex','session_id':'session-1',
                     'time_unix_nano':stamp+1,'domain':'message','action':'assistant','subject':'I will check the tests',
                     'detail':'I will check the tests first.','result':'observed'}
        other_session = {**prompt,'id':'prompt-2','source_id':'prompt-2','session_id':'session-2',
                         'subject':'Do not show this other agent'}
        feed.api.activity.extend([prompt, assistant, other_session])
        feed.session_id='session-1';feed.service='codex';feed.request_refresh()
        app = ObservatoryApp(feed)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause()
            app.query_one('#scope', Select).value='source-1'
            await pilot.pause()
            app.action_page('activity')
            await pilot.pause()
            await pilot.click('#prompts-activity')
            await pilot.pause()
            table = app.query_one('#activity-table', DataTable)
            self.assertEqual(table.row_count, 1)
            row = ' '.join(str(value) for value in table.get_row_at(0))
            self.assertIn('Deploy the update', row)
            self.assertNotIn('I will check the tests', row)
            self.assertNotIn('Do not show this other agent', row)
            self.assertEqual(len(feed.snapshot.activity), 3)

    def test_recorded_progress_uses_captured_facts_and_does_not_guess_remaining_work(self):
        item = {'state': 'Working', 'stamp': time.time_ns(), 'waiting_for': ''}
        events = [{'time_unix_nano': time.time_ns(), 'domain': 'file', 'action': 'write',
                   'subject': 'src/app.py', 'result': 'success'}]
        summary = recorded_progress(item, events, {})
        self.assertIn('Latest: Write app.py', summary)
        self.assertIn('Errors: 0 recorded in captured recent activity', summary)
        self.assertIn('Remaining work is not reported by the agent.', summary)

    def test_discovery_caps_are_visible_as_partial_coverage(self):
        warning = coverage_warning({'omitted_sources': 3, 'max_sources': 50,
                                    'opencode_omitted_sessions': 2})
        self.assertIn('3 older Codex/Claude sessions', warning)
        self.assertIn('2 older OpenCode sessions', warning)
        self.assertEqual(coverage_warning({}), '')

    async def test_server_inventory_includes_older_servers_and_filters_calls(self):
        feed = ManualFeed()
        feed.snapshot.mcp_summary = {'calls': 140, 'tools': 5, 'errors': 2,
            'servers': [{'server': 'github', 'calls': 139, 'tools': 4, 'errors': 1},
                        {'server': 'older-server', 'calls': 1, 'tools': 1, 'errors': 1}],
            'metric_calls': 68, 'metric_servers': [
                {'server': 'cursor-mcp-server', 'calls': 68, 'tools': 3, 'errors': 2}]}
        app = ObservatoryApp(feed)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_page('mcp')
            await pilot.pause()
            table = app.query_one('#mcp-servers', DataTable)
            self.assertEqual(table.row_count, 3, repr(app.table_data.get('mcp-servers')))
            aggregate = app.table_data['mcp-servers']['cursor-mcp-server']
            self.assertEqual((aggregate['calls'], aggregate['metric_calls']), (0, 68))
            row_index = [key.value for key in table.rows].index('cursor-mcp-server')
            self.assertIn('Aggregate metric', str(table.get_row_at(row_index)))
            table.focus()
            table.move_cursor(row=2)
            await pilot.press('enter')
            await pilot.pause()
            self.assertEqual(app.query_one('#search-mcp', Input).value, 'older-server')

    async def test_unattributed_mcp_calls_are_visible_and_filterable_in_inventory(self):
        feed = ManualFeed()
        feed.api.mcp.append({
            'id': 'call-unattributed', 'service': 'codex', 'session_id': 'session-1',
            'time_unix_nano': time.time_ns(), 'server': 'Not reported', 'tool': 'lookup_release',
            'method': 'tools/call', 'status': 'OK', 'duration_ms': 12,
            'arguments': {'query': 'release'}, 'output': {'matches': 1},
            'source': 'span', 'trace_id': 'trace-unattributed', 'span_id': 'span-unattributed'})
        feed.api.mcp_summary = {'calls': 2, 'tools': 2, 'errors': 0,
            'servers': [{'server': 'github', 'calls': 1, 'tools': 1, 'errors': 0}],
            'unknown_server_calls': 1, 'unknown_server_tools': 1, 'unknown_server_errors': 0}
        feed.extra_updated = 0
        feed.refresh()
        app = ObservatoryApp(feed)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_page('mcp')
            await pilot.pause()
            table = app.query_one('#mcp-servers', DataTable)
            self.assertIn('Not reported', app.table_data['mcp-servers'])
            row_index = [key.value for key in table.rows].index('Not reported')
            self.assertIn('attribution unavailable', str(table.get_row_at(row_index)))
            table.focus()
            table.move_cursor(row=row_index)
            await pilot.press('enter')
            await pilot.pause(0.3)
            self.assertEqual(app.query_one('#search-mcp', Input).value, 'Not reported')
            self.assertIn('call-unattributed', app.table_data.get('mcp-table', {}))

    async def test_non_tool_mcp_method_is_visible_in_the_operations_column(self):
        feed = ManualFeed()
        feed.api.mcp[0].update(tool='Not reported', method='resources/read')
        feed.extra_updated = 0
        feed.refresh()
        app = ObservatoryApp(feed)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_page('mcp')
            await pilot.pause()
            table = app.query_one('#mcp-table', DataTable)
            self.assertIn('Tool / method', [str(column.label) for column in table.columns.values()])
            row = ' '.join(str(value) for value in table.get_row_at(0))
            self.assertIn('resources/read', row)

    async def test_global_findings_are_searchable_and_open_the_source_trace(self):
        feed = ManualFeed()
        feed.api.findings = [{
            'id': 'finding-credential-001', 'trace_id': 'trace-1', 'kind': 'credential_exposure',
            'title': 'Possible credential material captured', 'severity': 'high',
            'detail': 'Detected token pattern in captured text. Values are omitted.',
            'evidence': [{'type': 'span', 'id': 'span-1'}], 'time_unix_nano': time.time_ns(),
            'session_id': 'session-1', 'task': 'Review migration', 'services': ['codex'],
            'occurrence_count': 3, 'trace_count': 2, 'session_count': 1}]
        feed.findings_cache = None
        feed.refresh()
        app = ObservatoryApp(feed)
        async with app.run_test(size=(150, 42)) as pilot:
            await pilot.pause()
            app.action_page('findings')
            await pilot.pause()
            table = app.query_one('#finding-table', DataTable)
            self.assertEqual(table.row_count, 1)
            self.assertIn('credential_exposure', str(table.get_row_at(0)))
            self.assertIn('Possible credential material', app.query_one('#finding-detail').plain_text)
            self.assertIn('3 across 2 traces', app.query_one('#finding-detail').plain_text)
            self.assertFalse(app.query_one('#finding-trace').disabled)
            await pilot.click('#finding-trace')
            await pilot.pause()
            self.assertEqual(feed.selected_trace, 'trace-1')
            self.assertEqual(feed.selected_span_id, 'span-1')
            self.assertEqual(app.query_one('#pages').active, 'traces')

    async def test_configured_server_without_calls_is_listed_as_unverified(self):
        feed = ManualFeed()
        feed.api.mcp_configurations = [
            {'server': 'not-called-yet', 'client': 'Codex', 'scope': 'User', 'config': 'config.toml'}]
        feed.refresh()
        self.assertEqual(len(feed.snapshot.local_status['mcp_configurations']), 1)
        app = ObservatoryApp(feed)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_page('mcp')
            await pilot.pause()
            table = app.query_one('#mcp-servers', DataTable)
            self.assertEqual(table.row_count, 2, repr(app.table_data.get('mcp-servers')))
            rows = app.table_data['mcp-servers']
            entry = rows['not-called-yet']
            self.assertEqual(entry['calls'], 0)
            self.assertEqual(entry['server'], 'not-called-yet')
            row_index = [key.value for key in table.rows].index('not-called-yet')
            self.assertIn('not-called-yet', str(table.get_row_at(row_index)))
            self.assertIn('Configured · Codex', str(table.get_row_at(row_index)))

    def test_stale_running_session_does_not_claim_live_or_approval_wait(self):
        feed = ManualFeed()
        entry = feed.snapshot.local_status['sessions'][0]
        entry.update(run_state='running', waiting_for='Tool response: github / search')
        entry['last_activity']['time_unix_nano'] = time.time_ns() - 120_000_000_000
        item = next(item for item in sources(feed.snapshot) if item['id'] == entry['source_id'])
        self.assertEqual(item['state'], 'No updates')
        self.assertFalse(item['recent'])
        self.assertNotIn('approval', item['waiting_for'].lower())

    def test_agent_list_includes_recent_otlp_sessions_without_connection_registration(self):
        feed = ManualFeed()
        feed.snapshot.sessions = [{
            'session_id': 'gemini-native-session', 'services': ['gemini-cli'],
            'models': ['gemini-2.5-pro'], 'latest_status': 'WAITING',
            'latest_task': 'Inspect failing tests', 'last_unix_nano': time.time_ns(),
            'waiting_for': 'Approval', 'span_count': 3, 'trace_count': 1,
            'project': 'agent-goechala'}]
        item = next(row for row in sources(feed.snapshot) if row.get('session_id') == 'gemini-native-session')
        self.assertEqual(item['runtime'], 'Gemini CLI')
        self.assertEqual(item['state'], 'Waiting')
        self.assertEqual(item['waiting_for'], 'Approval')
        self.assertEqual(item['project'], 'agent-goechala')
        self.assertEqual(item['model'], 'gemini-2.5-pro')
        self.assertEqual(item['spans'], 3)
        feed.snapshot.sessions[0].update(latest_status='RUNNING', waiting_for='')
        item = next(row for row in sources(feed.snapshot) if row.get('session_id') == 'gemini-native-session')
        self.assertEqual(item['state'], 'Working')
