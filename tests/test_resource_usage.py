"""Deterministic work budgets, rather than machine-dependent CPU assertions."""
import unittest
from unittest.mock import Mock, patch

from observatory.terminal import Feed
from observatory.terminal_ui import ObservatoryApp
from tests.test_terminal_search import ExampleApi, ManualFeed


class FeedWorkBudgetTests(unittest.TestCase):
    def test_inventory_queries_are_cached_between_live_refreshes(self):
        api = ExampleApi()
        feed = Feed(api)
        with patch('observatory.terminal.time.monotonic', return_value=10):
            feed.refresh()
        api.requests.clear()
        with patch('observatory.terminal.time.monotonic', return_value=14):
            feed.refresh()
        self.assertIn('/api/health', api.requests)
        for route in ('/api/sessions', '/api/mcp', '/api/findings', '/api/usage-series',
                      '/api/metric-catalog', '/api/measurements'):
            self.assertFalse(any(path.split('?')[0] == route for path in api.requests), route)
        api.requests.clear()
        with patch('observatory.terminal.time.monotonic', return_value=16):
            feed.refresh()
        self.assertIn('/api/sessions?limit=500', api.requests)

    def test_filter_changes_bypass_inventory_cache(self):
        feed = Feed(ExampleApi())
        with patch('observatory.terminal.time.monotonic', return_value=10):
            feed.refresh()
            feed.api.requests.clear()
            feed.session_id = 'different-session'
            feed.refresh()
        self.assertTrue(any('session_id=different-session' in path
                            for path in feed.api.requests if path.startswith('/api/mcp?')))

    def test_polling_waits_and_manual_refresh_wakes_it(self):
        feed = Feed(ExampleApi())
        feed.stop = Mock()
        feed.stop.is_set.side_effect = [False, True]
        feed.wake = Mock()
        with patch.object(feed, 'refresh') as refresh:
            feed._loop()
        refresh.assert_called_once()
        feed.wake.wait.assert_called_once_with(2)
        feed.request_refresh()
        feed.wake.set.assert_called_once()


class RenderingWorkBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_changed_cell_does_not_repaint_the_entire_row(self):
        feed = ManualFeed()
        app = ObservatoryApp(feed)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            app.refresh_timer.stop()
            # Hidden tabs deliberately do no view-model/render work now.
            app.action_page('activity')
            await pilot.pause()
            table = app.query_one('#activity-table')
            feed.api.activity[0]['subject'] = 'one changed command'
            feed.refresh()
            with patch.object(table, 'update_cell', wraps=table.update_cell) as update:
                app.refresh_data()
            self.assertEqual(update.call_count, 1)
            with patch.object(table, 'update_cell', wraps=table.update_cell) as update:
                feed.refresh()
                app.refresh_data()
            update.assert_not_called()
