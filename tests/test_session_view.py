import time
import unittest
from unittest.mock import patch

from textual.widgets import DataTable, Input
from observatory.terminal_ui import ObservatoryApp, sources
from tests.test_terminal_search import ManualFeed


class SessionViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_columns_waiting_and_summary_are_bound_to_selected_session(self):
        feed = ManualFeed()
        entry = feed.snapshot.local_status['sessions'][0]
        entry.update(run_state='waiting', waiting_for='User input', working_directory='/work/payments', branch='fix/mcp')
        app = ObservatoryApp(feed)
        with patch('observatory.ai_summary.generate_summary', return_value='Reviewed migration. Waiting for your answer.') as generate:
            async with app.run_test(size=(160, 48)) as pilot:
                await pilot.pause()
                table = app.query_one('#agent-table', DataTable)
                self.assertEqual([str(c.label) for c in table.columns.values()],
                    ['Agent', 'Session ID', 'Repo / project', 'Status', 'Updated', 'Waiting on', 'Task / details'])
                row = ' '.join(str(value) for value in table.get_row_at(0))
                self.assertIn('Waiting', row)
                self.assertIn('User input', row)
                # Mounting and refreshes never trigger paid API calls.
                generate.assert_not_called()
                with patch.dict('os.environ', {'OBSERVATORY_SUMMARY_URL': 'http://localhost:1234/v1/chat/completions',
                                               'OBSERVATORY_SUMMARY_MODEL': 'local-model'}):
                    await pilot.click('#agent-summary')
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertIn(entry['source_id'], app.ai_summaries)
                    self.assertEqual(generate.call_count, 1)
                    await pilot.click('#agent-summary')
                    await app.workers.wait_for_complete()
                    self.assertEqual(generate.call_count, 1)
                    self.assertEqual(generate.call_args.args[1]['project'], 'payments')

    async def test_server_inventory_includes_older_servers_and_filters_calls(self):
        feed = ManualFeed()
        feed.snapshot.mcp_summary = {'calls': 140, 'tools': 5, 'errors': 2,
            'servers': [{'server': 'github', 'calls': 139, 'tools': 4, 'errors': 1},
                        {'server': 'older-server', 'calls': 1, 'tools': 1, 'errors': 1}]}
        app = ObservatoryApp(feed)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_page('mcp')
            await pilot.pause()
            table = app.query_one('#mcp-servers', DataTable)
            self.assertEqual(table.row_count, 2)
            table.focus()
            table.move_cursor(row=1)
            await pilot.press('enter')
            await pilot.pause()
            self.assertEqual(app.query_one('#search-mcp', Input).value, 'older-server')

    def test_stale_running_session_does_not_claim_live_or_approval_wait(self):
        feed = ManualFeed()
        entry = feed.snapshot.local_status['sessions'][0]
        entry.update(run_state='running', waiting_for='Tool response: github / search')
        entry['last_activity']['time_unix_nano'] = time.time_ns() - 120_000_000_000
        item = next(item for item in sources(feed.snapshot) if item['id'] == entry['source_id'])
        self.assertEqual(item['state'], 'No updates')
        self.assertFalse(item['recent'])
        self.assertNotIn('approval', item['waiting_for'].lower())
