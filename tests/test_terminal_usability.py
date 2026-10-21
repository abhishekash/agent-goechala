"""Exercise what a person sees and copies, plus history under live arrivals."""
import asyncio
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from urllib.parse import parse_qs, urlsplit

from rich.text import Text
from textual.events import MouseDown, MouseMove, MouseUp
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Button, TabbedContent

from observatory.core import Store, normalize_native
from observatory.terminal import Feed
from observatory.terminal_ui import ObservatoryApp, write_system_clipboard
from tests.test_terminal_search import ExampleApi, ManualFeed


async def drag(app, pilot, log, start, end):
    for event_type, offset in ((MouseDown,start),(MouseMove,end),(MouseUp,end)):
        x,y=log.content_region.x+offset[0],log.content_region.y+offset[1]
        app.screen._forward_event(event_type(widget=None,x=x,y=y,
            delta_x=end[0]-start[0],delta_y=end[1]-start[1],button=1,
            shift=False,meta=False,ctrl=False,screen_x=x,screen_y=y))
        await pilot.pause()


class ControlVisibilityTest(unittest.IsolatedAsyncioTestCase):
    def assert_label_visible(self, button):
        self.assertGreater(button.content_size.height, 0,
                           f'{button.id}: borders and padding hid the label')
        rendered=' '.join(button.render_line(row).text for row in range(button.size.height))
        self.assertIn(button.label.plain, rendered, f'{button.id}: label missing from rendered button')

    async def test_selected_filters_keep_labels_visible_with_mouse_and_keyboard(self):
        app=ObservatoryApp(ManualFeed())
        async with app.run_test(size=(160,48)) as pilot:
            for size in ((160,48),(120,40),(80,24)):
                await pilot.resize_terminal(*size)
                app.action_all()
                app.action_page('activity')
                await pilot.pause()
                prompts=app.query_one('#prompts-activity',Button)
                prompts.focus()
                await pilot.press('enter')
                await pilot.pause()
                self.assertTrue(app.prompts_only)
                self.assert_label_visible(prompts)
                await pilot.hover(prompts)
                await pilot.pause()
                self.assert_label_visible(prompts)
                app.query_one('#activity-table').focus()
                await pilot.hover('#search-activity')
                await pilot.pause()
                self.assert_label_visible(prompts)
                # Errors replaces Prompts and uses the same selected-filter geometry.
                for page in ('activity','mcp'):
                    app.action_page(page)
                    await pilot.pause()
                    errors=app.query_one('#errors-'+page,Button)
                    await pilot.click(errors)
                    await pilot.pause(.2)
                    self.assertTrue(errors.has_class('-on'))
                    self.assert_label_visible(errors)
                    errors.focus()
                    await pilot.press('enter')
                    await pilot.pause()
                    self.assertFalse(errors.has_class('-on'))
                    self.assert_label_visible(errors)
                self.assertFalse(app.prompts_only)
                app.action_page('activity')
                await pilot.pause()
                self.assert_label_visible(prompts)

    async def test_visible_buttons_keep_labels_in_focus_hover_and_pressed_states(self):
        app=ObservatoryApp(ManualFeed())
        async with app.run_test(size=(160,48)) as pilot:
            for page in ('agents','activity','mcp','traces','host','metrics','findings'):
                app.action_page(page)
                await pilot.pause()
                for button in app.query(Button):
                    if not button.visible or not button.region.height:
                        continue
                    if not app.screen.region.contains_region(button.region):
                        continue
                    self.assert_label_visible(button)
                    if button.disabled:
                        continue
                    button.focus()
                    await pilot.hover(button)
                    await pilot.pause()
                    self.assert_label_visible(button)
                    button.add_class('-active')
                    await pilot.pause()
                    self.assert_label_visible(button)
                    button.remove_class('-active')


class PointerCopyTest(unittest.IsolatedAsyncioTestCase):
    async def test_history_buttons_and_search_input_copy(self):
        feed=ManualFeed();feed.api=PagedApi();feed.extra_cache=None;feed.refresh()
        app=ObservatoryApp(feed)
        async with app.run_test(size=(120,40)) as pilot:
            await pilot.pause();app.action_page('activity');await pilot.pause()
            await pilot.press(']');await pilot.pause()
            self.assertEqual(app.selected_activity,'older')
            self.assertFalse(app.query_one('#newer-activity').disabled)
            self.assertTrue(app.query_one('#older-activity').disabled)
            await pilot.click('#latest-activity');await pilot.pause()
            self.assertEqual(app.selected_activity,'latest')
            self.assertTrue(app.query_one('#newer-activity').disabled)
            field=app.query_one('#search-activity');field.value='git';field.focus()
            with patch('observatory.terminal_ui.write_system_clipboard',return_value='X11') as clipboard:
                await pilot.press('ctrl+a','ctrl+c');await app.workers.wait_for_complete();await pilot.pause()
                clipboard.assert_awaited_once_with('git')
                field.value='';await pilot.press('ctrl+v');await pilot.pause()
                self.assertEqual(field.value,'git')

    async def test_native_clipboard_detaches_output_and_sends_utf8(self):
        process=AsyncMock()
        process.returncode=0
        with patch('observatory.terminal_ui.os.environ', {'DISPLAY': ':0'}), \
             patch('observatory.terminal_ui.shutil.which', return_value='/usr/bin/xclip'), \
             patch('observatory.terminal_ui.asyncio.create_subprocess_exec',return_value=process) as launch:
            self.assertEqual(await write_system_clipboard('café 界'), 'X11')
        launch.assert_awaited_once_with('xclip','-selection','clipboard','-i',
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        process.communicate.assert_awaited_once_with('café 界'.encode('utf-8'))

    async def test_visible_selection_keyboard_copy_scroll_and_blank_space(self):
        feed=ManualFeed()
        app=ObservatoryApp(feed)
        async with app.run_test(size=(140,42)) as pilot:
            await pilot.pause()
            app.action_page('activity')
            await pilot.pause()
            log=app.query_one('#activity-detail')
            log.clear()
            log.write(Text('color café 界\nsecond line\n'+'\n'.join(f'line {i}' for i in range(50)),style='bold red'))
            await pilot.pause()
            await drag(app,pilot,log,(2,0),(6,1))
            selected=app.screen.get_selected_text()
            self.assertEqual(selected,'lor café 界\nsecond ' .rstrip())
            selection_style=app.screen.get_component_rich_style('screen--selection')
            self.assertNotEqual(selection_style.color,selection_style.bgcolor)
            self.assertTrue(any(segment.style and segment.style.bgcolor==selection_style.bgcolor
                                for segment in log.render_line(0)), 'Selection must be visible')
            with patch('observatory.terminal_ui.write_system_clipboard',return_value='X11') as clipboard:
                await pilot.press('ctrl+c')
                await app.workers.wait_for_complete()
                await pilot.pause()
                clipboard.assert_called_once_with(selected)
            self.assertNotIn('\x1b',selected)
            # A live refresh must not replace the content under the drag.
            feed.api.activity.insert(0,{**feed.api.activity[0],'source_id':'new','subject':'new event'})
            feed.refresh(); app.refresh_data()
            self.assertEqual(app.screen.get_selected_text(),selected)
            await pilot.press('escape')
            self.assertIsNone(app.screen.get_selected_text())
            # A source replacement clears selection; blank space never crashes.
            log.clear();log.write(Text('\n'.join(f'line {i}' for i in range(50)),style='red'))
            await pilot.pause();log.scroll_to(y=20,animate=False);await pilot.pause()
            await drag(app,pilot,log,(0,0),(5,0))
            self.assertEqual(app.screen.get_selected_text(),'line 2')
            self.assertEqual(log.get_selection(Selection(Offset(0,100),Offset(10,100)))[0],'')
            log.clear();log.write('replacement')
            self.assertIsNone(app.screen.get_selected_text())

    async def test_trace_key_follows_the_selected_session_from_the_agents_page(self):
        feed=ManualFeed()
        app=ObservatoryApp(feed)
        async with app.run_test(size=(160,48)) as pilot:
            await pilot.pause()
            app.action_page('agents')
            await pilot.pause()
            self.assertEqual(app.query_one('#pages',TabbedContent).active,'agents')
            await pilot.press('t')
            await pilot.pause()
            self.assertEqual(app.query_one('#pages',TabbedContent).active,'traces')
            self.assertEqual(feed.selected_trace,'trace-1')
            await pilot.press('escape')
            await pilot.pause()
            self.assertEqual(app.query_one('#pages',TabbedContent).active,'agents')

    async def test_trace_key_reports_when_a_session_has_no_linked_trace(self):
        feed=ManualFeed()
        feed.api.traces=[]
        feed.api.sessions=[{'source_id':'source-9','session_id':'session-9','service':'codex',
                            'runtime':'Codex','task':'Unrelated work','project':'other','model':'model-x'}]
        feed.refresh()
        app=ObservatoryApp(feed)
        async with app.run_test(size=(160,48)) as pilot:
            await pilot.pause()
            app.action_page('agents')
            await pilot.pause()
            await pilot.press('t')
            await pilot.pause()
            self.assertEqual(app.query_one('#pages',TabbedContent).active,'agents')
            self.assertTrue(app._notifications)

    async def test_trace_back_preserves_event_and_scroll_during_new_arrivals(self):
        feed=ManualFeed()
        base=feed.api.activity[0]
        feed.api.activity=[{**base,'source_id':f'event-{i}','subject':f'event {i}',
                            'trace_id':'trace-1','span_id':'span-2',
                            'detail':'\n'.join(f'output {n}' for n in range(80))}
                           for i in range(30)]
        feed.refresh()
        app=ObservatoryApp(feed)
        async with app.run_test(size=(160,48)) as pilot:
            await pilot.pause();app.action_page('activity');await pilot.pause()
            table=app.query_one('#activity-table');table.move_cursor(row=15)
            await pilot.pause()
            log=app.query_one('#activity-detail');log.scroll_to(y=18,animate=False)
            await pilot.pause()
            selected=app.selected_activity
            scroll=log.scroll_offset
            await pilot.press('t');await pilot.pause()
            self.assertEqual(app.query_one('#pages',TabbedContent).active,'traces')
            await pilot.press('escape');await pilot.pause()
            self.assertEqual(app.query_one('#pages',TabbedContent).active,'activity')
            self.assertEqual(app.selected_activity,selected)
            self.assertEqual(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value,selected)
            self.assertEqual(log.scroll_offset,scroll)
            # Even if the event ages out of the newest page, retain it for reading.
            feed.api.activity=[{**base,'source_id':'newest','subject':'arrived later'}]
            feed.refresh();app.refresh_data();await pilot.pause()
            self.assertEqual(app.selected_activity,selected)
            self.assertIn(selected,app.table_data['activity-table'])
            self.assertEqual(log.scroll_offset,scroll)
            with patch('observatory.terminal_ui.write_system_clipboard',return_value='X11') as clipboard:
                await pilot.press('c');await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIn('event 15',clipboard.call_args.args[0])


class HistoryStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'history.db')
    def tearDown(self):
        self.temp.cleanup()

    def log(self,number,trace='a'*32,span=None,attrs=None):
        return {'trace_id':trace,'span_id':span,'time_unix_nano':number,'service':'codex',
                'severity_number':9,'severity_text':'INFO','event_name':'agent.activity',
                'body':f'old output {number}', 'attributes':{'session.id':'session-old',
                'agent.activity.domain':'tool','agent.activity.subject':f'action {number}',**(attrs or {})},
                'resource_attributes':{},'scope_name':'history-test'}

    def test_activity_pages_include_old_traces_and_all_saved_logs(self):
        self.store.ingest_logs([self.log(i) for i in range(1105)])
        self.store.ingest([normalize_native({'trace_id':f'{i+100:032x}','span_id':'1'*16,
            'name':'tool','start_unix_nano':2000+i,'end_unix_nano':2001+i,
            'attributes':{'tool.name':'recent-tool'},'events':[]}) for i in range(105)])
        oldest=self.store.activity_page(query='old output 0')
        self.assertEqual(len(oldest['activity']),1)
        first=self.store.activity_page(limit=200,session_id='session-old')
        keys=[item['id'] for item in first['activity']]
        self.store.ingest_logs([self.log(9999)])  # Arrives between page reads.
        cursor=first['next_cursor']
        while cursor:
            page=self.store.activity_page(limit=200,session_id='session-old',cursor=cursor)
            keys.extend(item['id'] for item in page['activity'])
            cursor=page['next_cursor']
        self.assertEqual(len(keys),1105)
        self.assertEqual(len(keys),len(set(keys)))
        with self.assertRaises(ValueError):self.store.activity_page(cursor='invalid')

    def test_mcp_pages_keep_span_and_log_evidence_together_and_search_history(self):
        spans=[];logs=[]
        for i in range(8):
            span=f'{i+1:016x}'
            attrs={'mcp.server.name':'github','mcp.tool.name':f'tool-{i}',
                   'input.value':json.dumps({'query':f'needle-{i}'})}
            spans.append(normalize_native({'trace_id':'a'*32,'span_id':span,'name':f'tool-{i}',
                'start_unix_nano':100+i,'end_unix_nano':200+i,'attributes':attrs,'events':[]},service='codex'))
            logs.append(self.log(400+i,span=span,attrs={**attrs,'agent.activity.result':'success'}))
        self.store.ingest(spans);self.store.ingest_logs([*logs,{**logs[0],'span_id':None}])
        self.assertEqual(self.store.mcp_page(limit=2,query='needle-0')['calls'][0]['tool'],'tool-0')
        calls=[];cursor=None
        while True:
            page=self.store.mcp_page(limit=2,cursor=cursor)
            calls.extend(page['calls'])
            cursor=page['next_cursor']
            if not cursor:break
        self.assertEqual(len(calls),8)
        self.assertEqual(len({call['id'] for call in calls}),8)
        self.assertTrue(all(call['source']=='session log + span' for call in calls))
        self.assertTrue(all(call['arguments'] for call in calls))


class PagedApi(ExampleApi):
    def get(self,path):
        parsed=urlsplit(path);params=parse_qs(parsed.query)
        response=super().get(path)
        if parsed.path=='/api/activity':
            cursor=params.get('cursor',[''])[0]
            base=self.activity[0]
            response={'activity':[{**base,'source_id':'older' if cursor else 'latest'}],
                      'next_cursor':None if cursor else 'older-page'}
        elif parsed.path in {'/api/mcp','/api/traces'}:
            response={**response,'next_cursor':None}
        return response


class FeedHistoryTest(unittest.TestCase):
    def test_older_newer_latest_and_filter_reset(self):
        feed=Feed(PagedApi());feed.refresh()
        self.assertTrue(feed.navigate_history('activity','older'));feed.refresh()
        self.assertEqual(feed.snapshot.activity_results[0]['source_id'],'older')
        self.assertEqual(feed.snapshot.pagination['activity']['page'],2)
        self.assertEqual(feed.snapshot.activity[0]['source_id'],'latest')
        self.assertTrue(feed.navigate_history('activity','newer'));feed.refresh()
        self.assertEqual(feed.snapshot.activity_results[0]['source_id'],'latest')
        feed.navigate_history('activity','older');feed.refresh()
        feed.activity_query='new query';feed.refresh()
        self.assertEqual(feed.snapshot.pagination['activity']['page'],1)
        self.assertEqual(feed.history['activity'],[None])
