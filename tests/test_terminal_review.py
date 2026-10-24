"""Focused regressions for resize, metric navigation, and in-flight filters."""
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Button

from observatory.terminal import Feed, Snapshot
from observatory.terminal_ui import ObservatoryApp, ResultsTable, sources
from tests.test_terminal_search import ExampleApi, ManualFeed


class FeedFilterRaceTest(unittest.TestCase):
    def test_prompt_toggle_during_poll_discards_old_snapshot(self):
        api=ExampleApi()
        feed=Feed(api)
        feed.refresh()
        previous=feed.snapshot
        original=api.get

        def get(path):
            if urlsplit(path).path=='/api/traces':
                feed.activity_prompts=True
            return original(path)

        with patch.object(api,'get',side_effect=get):
            feed.refresh()
        self.assertIs(feed.snapshot,previous)
        self.assertTrue(feed.wake.is_set())
        feed.refresh()
        self.assertTrue(feed.snapshot.activity_prompts)
        self.assertEqual(feed.snapshot.activity_results,[])
        self.assertIn('domain=message','\n'.join(api.requests))

    def test_prompt_scope_is_captured_before_http_reads(self):
        api=ExampleApi()
        feed=Feed(api)
        original=api.get

        def get(path):
            if urlsplit(path).path=='/api/health':
                feed.activity_prompts=True
            return original(path)

        with patch.object(api,'get',side_effect=get):
            feed.refresh()
        self.assertFalse(any('domain=message' in path for path in api.requests))
        self.assertEqual(feed.snapshot.updated,0)
        self.assertTrue(feed.wake.is_set())


class SessionWorkBudgetTest(unittest.TestCase):
    def test_activity_is_indexed_once_for_all_sessions(self):
        class CountedEvents(list):
            visits=0

            def __iter__(self):
                for event in super().__iter__():
                    self.visits+=1
                    yield event

        now=time.time_ns()
        events=CountedEvents([
            {'session_id':str(index),'time_unix_nano':now,'subject':'latest'}
            for index in range(200)
        ]+[
            {'session_id':str(index),'time_unix_nano':now-1,'subject':'older'}
            for index in range(200)
        ])
        snapshot=Snapshot(activity=events,sessions=[
            {'session_id':str(index),'services':['demo'],'last_unix_nano':now}
            for index in range(500)
        ])
        result=sources(snapshot)
        self.assertEqual(events.visits,len(events))
        self.assertEqual(len(result),500)
        session=next(item for item in result if item['session_id']=='42')
        self.assertEqual(session['last_activity']['subject'],'latest')


class ResizeApp(App):
    def compose(self)->ComposeResult:
        yield ResultsTable(id='agent-table',cursor_type='row')

    def on_mount(self):
        table=self.query_one(ResultsTable)
        table.configure([('Agent',8,0),('Task',10,1)])
        for index in range(3):
            table.add_row(Text('Demo'),Text('x'*120),key=str(index))
        table.move_cursor(row=1)


class TerminalReviewHeadlessTest(unittest.IsolatedAsyncioTestCase):
    async def test_resize_rewraps_previews_and_preserves_selection(self):
        app=ResizeApp()
        async with app.run_test(size=(180,30)) as pilot:
            await pilot.pause()
            table=app.query_one(ResultsTable)
            self.assertEqual(table.rows['1'].height,1)
            await pilot.resize_terminal(80,30)
            await pilot.pause()
            self.assertEqual(table.rows['1'].height,2)
            self.assertEqual(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value,'1')
            await pilot.resize_terminal(180,30)
            await pilot.pause()
            self.assertEqual(table.rows['1'].height,1)
            self.assertEqual(table.cursor_row,1)

    async def test_metric_shortcut_ignores_other_instruments_and_stale_history(self):
        feed=ManualFeed()
        original=feed.api.metric_catalog[0]
        other={**original,'name':'other.instrument'}
        feed.api.metric_catalog.append(other)
        feed.refresh()
        app=ObservatoryApp(feed)
        async with app.run_test(size=(160,48)) as pilot:
            await pilot.pause()
            app.action_page('metrics')
            await pilot.pause()
            key=next(key for key,item in app.table_data['metrics-table'].items()
                     if item['name']=='other.instrument')
            app.selected_metric=key
            app.metric_history_loaded_key='previous-instrument'
            app.metric_history_points=feed.snapshot.metric_points
            app.last_update=-1
            app.refresh_data()
            self.assertTrue(app.query_one('#metrics-trace',Button).disabled)
            with patch.object(feed,'request_refresh') as refresh:
                await pilot.press('t')
                await pilot.pause()
            refresh.assert_not_called()
            self.assertEqual(app.query_one('#pages').active,'metrics')
            app.metric_history_loaded_key=key
            app.metric_history_points=[{'data':{'exemplars':[{'trace_id':'right-trace','span_id':'right-span'}]}}]
            app.action_open_trace()
            self.assertEqual(feed.selected_trace,'right-trace')
            self.assertEqual(feed.selected_span_id,'right-span')

    async def test_prompts_wait_for_matching_snapshot(self):
        feed=ManualFeed()
        app=ObservatoryApp(feed)
        async with app.run_test(size=(160,48)) as pilot:
            await pilot.pause()
            app.action_page('activity')
            await pilot.pause()
            with patch.object(feed,'request_refresh'):
                await pilot.click('#prompts-activity')
                await pilot.pause()
            self.assertEqual(app.query_one('#activity-table').row_count,0)
            self.assertIn('Filtering',str(app.query_one('#search-status-activity').render()))
            self.assertIn('Filtering activity',app.query_one('#activity-detail').plain_text)
            feed.refresh()
            app.refresh_data()
            self.assertEqual(app.query_one('#activity-table').row_count,0)
            self.assertIn('No user prompts',app.query_one('#activity-detail').plain_text)


if __name__=='__main__':
    unittest.main()
