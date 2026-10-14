"""Agent-first terminal UI. Raw evidence lives in scrollable inspectors."""
from __future__ import annotations

import json
import shlex
import time
import webbrowser
from pathlib import Path

from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, RichLog, Select, Static, TabbedContent, TabPane, Tree

from .terminal import clean, number, trace_name


def cell(value, style=''):
    return Text(clean(value), style=style, overflow='ellipsis', no_wrap=True)


def age(stamp):
    if not stamp:
        return 'unknown'
    seconds=max(0,int(time.time()-float(stamp)/1e9))
    return f'{seconds}s ago' if seconds<60 else f'{seconds//60}m ago' if seconds<3600 else f'{seconds//3600}h ago'


def timing(value):
    try:
        if value is None:
            return 'not reported'
        value=float(value)
        return f'{value:.0f} ms' if value<1000 else f'{value/1000:.2f} s' if value<60000 else f'{value/60000:.1f} min'
    except (ValueError,TypeError):
        return 'not reported'


def quantity(value, singular, plural=None):
    return f"{value} {singular if value==1 else (plural or singular+'s')}"


def when(stamp):
    value=time.localtime(stamp/1e9)
    return time.strftime('%H:%M:%S' if value[:3]==time.localtime()[:3] else '%m-%d %H:%M',value)


def table_when(stamp):
    value=time.localtime(stamp/1e9)
    return time.strftime('%H:%M:%S' if value[:3]==time.localtime()[:3] else '%m-%d',value)


def status(value):
    label={'OK':'OK','UNSET':'Observed','RUNNING':'In progress','ERROR':'Error','FAILED':'Failed','INCOMPLETE':'Incomplete',
           'success':'OK','started':'Started','completed':'Completed','observed':'Observed','reported':'Reported','error':'Error'}.get(str(value),str(value).title())
    color='#ff8b92' if str(value).lower() in {'error','failed','denied','blocked','incomplete'} else '#6ee7c2' if str(value).lower() in {'success','ok','completed'} else '#8bbdff' if str(value).lower() in {'running','started'} else '#8b9bb0'
    return cell(label,color)


def decode(value):
    # Local logs often wrap the same event in several JSON-encoded transport
    # envelopes. Unwrap those so newlines and typed text blocks reach the UI.
    for _ in range(24):
        if not isinstance(value,str):
            break
        try:
            parsed=json.loads(value)
        except (ValueError,TypeError):
            break
        if isinstance(parsed,(dict,list)):
            value=parsed
        elif isinstance(parsed,str) and parsed.lstrip().startswith(('{','[')):
            # Some event producers JSON-encode the payload, then JSON-encode
            # that encoded string again inside their text block.
            value=parsed
        else:
            break
    return value


def decode_nested(value,depth=0):
    """Decode JSON strings inside arbitrarily nested telemetry fields."""
    value=decode(value)
    if depth>=64:return value
    if isinstance(value,dict):
        return {key:decode_nested(child,depth+1) for key,child in value.items()}
    if isinstance(value,list):
        return [decode_nested(child,depth+1) for child in value]
    return value


def shell_command(value):
    """Remove the login-shell wrapper from commands captured from a PTY."""
    command=str(value).strip()
    try:
        args=shlex.split(command)
    except ValueError:
        return command
    if len(args)>=3 and Path(args[0]).name in {'sh','bash','zsh','fish'} and args[1] in {'-c','-lc','-ilc'}:
        return args[2]
    return command


def heredoc_script(command):
    """Return the language and body for a captured interpreter heredoc."""
    lines=command.splitlines()
    if len(lines)<3:
        return None
    try:
        header=shlex.split(lines[0])
    except ValueError:
        return None
    interpreter=Path(header[0]).name.lower() if header else ''
    delimiter_arg=next((part for part in header if part.startswith('<<')),None)
    if not delimiter_arg:
        return None
    delimiter=delimiter_arg[2:].strip("'\"")
    if not delimiter or lines[-1].strip()!=delimiter:
        return None
    if interpreter.startswith('python'):
        language,title='python','Python script'
    elif interpreter in {'node','nodejs','deno','bun'}:
        language,title='javascript','JavaScript script'
    elif interpreter in {'bash','sh','zsh','fish'}:
        language,title='bash','Shell script'
    else:
        language,title='text',f'{interpreter or "Interpreter"} script'
    return language,title,'\n'.join(lines[1:-1])


def write_json(log,value):
    """Render indented JSON and move multiline string values into readable blocks."""
    text_values=[]
    def prepare(item,path=''):
        if isinstance(item,dict):
            return {key:prepare(child,f'{path}.{key}' if path else str(key)) for key,child in item.items()}
        if isinstance(item,list):
            return [prepare(child,f'{path}[{index}]') for index,child in enumerate(item)]
        if isinstance(item,str) and '\n' in item:
            text_values.append((path or 'Text value',item))
            line_count=len(item.splitlines())
            return f'[multiline text · {line_count} lines shown below]'
        return item

    formatted=json.dumps(prepare(value),ensure_ascii=False,indent=2)
    log.write(Syntax(formatted,'json',word_wrap=True,background_color='#101720'))
    for path,content in text_values:
        log.write(Text(f'\n{path} · {len(content.splitlines())} lines',style='bold #92b9eb'))
        log.write(Text(content))


def headline(item):
    domain=item.get('domain') or 'tool'
    subject=str(item.get('subject') or item.get('action') or 'Observed activity')
    if domain=='tool' and subject.casefold() in {'exec','execute','shell'}:
        details=decode_nested(item.get('detail') or '')
        def find_command(value):
            if isinstance(value,dict):
                for key,child in value.items():
                    if str(key).casefold() in {'command','cmd'} and isinstance(child,str):
                        return child
                for child in value.values():
                    found=find_command(child)
                    if found:return found
            elif isinstance(value,list):
                for child in value:
                    found=find_command(child)
                    if found:return found
            return None
        command=find_command(details)
        if command:
            command=shell_command(command)
            script=heredoc_script(command)
            if script:return script[1]
            first=command.splitlines()[0] if command.splitlines() else command
            return clean(first)
    if domain=='shell':
        try:
            args=shlex.split(subject)
            if len(args)>=3 and args[1] in {'-lc','-c','-ilc'}:
                subject=args[2]
        except ValueError:
            pass
        first=subject.splitlines()[0] if subject.splitlines() else 'Shell command'
        if ' - <<' in first and first.startswith(('python','python3')):
            return 'Run Python script'
        return clean(first)
    if domain=='file':
        return f"{str(item.get('action') or 'Change').title()} {Path(subject).name}"
    if domain=='mcp':
        return f"{item.get('mcp_server') or subject} / {item.get('mcp_tool') or item.get('action')}"
    if domain=='message':
        return ('User: ' if item.get('action')=='user' else 'Agent: ')+subject
    if domain=='model':
        return 'Model usage · '+subject
    if domain=='agent':
        return {'processing':'Model processing','started':'Turn started','completed':'Turn completed','interrupted':'Turn interrupted','compaction':'Context compacted'}.get(item.get('action'),subject)
    return subject if item.get('action') in {'call','result','execute_tool'} else f"{item.get('action') or domain} · {subject}"


def sources(snapshot, include_reviews=False):
    connections={item['source_id']:item for item in snapshot.agents}
    result=[]
    for entry in snapshot.local_status.get('sessions',[]):
        connection=connections.pop(entry['source_id'],{})
        review=entry.get('service')=='codex-review' or entry.get('model')=='codex-auto-review'
        if review and not include_reviews:
            continue
        last=entry.get('last_activity') or {}
        stamp=last.get('time_unix_nano') or 0
        recent=bool(last and time.time()-stamp/1e9<35)
        result.append({**entry,'id':entry['source_id'],'review':review,'recent':recent,'stamp':stamp,
                       'state':'Recent' if recent else 'Idle','spans':connection.get('span_count',0)})
    for entry in connections.values():
        if 'local-session-log' in entry.get('integrations',[]):
            continue
        result.append({**entry,'id':entry['source_id'],'session_id':None,'project':'SDK / OTLP','model':'',
                       'runtime':entry.get('service'),'task':'Task not reported','recent':entry['state']=='running',
                       'state':'Connected' if entry['state']=='running' else 'Finished' if entry['state']=='stopped' else 'Offline',
                       'stamp':entry.get('last_seen_ns',0),'spans':entry.get('span_count',0),'review':False})
    return sorted(result,key=lambda item:(not item['recent'],-item['stamp'],item['id']))


class ResultsTable(DataTable):
    """Size columns to the panel while keeping the selected record on resize."""
    def configure(self, columns):
        self.column_specs=columns
        self.column_widths=()
        self.fit_columns()

    def on_resize(self):
        self.fit_columns()

    def fit_columns(self):
        specs=getattr(self,'column_specs',None)
        if not specs:return
        available=max(0,self.scrollable_content_region.width-2*len(specs)-1)
        widths=[minimum for _,minimum,_ in specs]
        spare=max(0,available-sum(widths))
        weight=sum(weight for _,_,weight in specs)
        for index,(_,_,share) in enumerate(specs):
            widths[index]+=int(spare*share/weight)
        widths[max(range(len(specs)),key=lambda i:specs[i][2])]+=max(0,available-sum(widths))
        widths=tuple(widths)
        if widths==self.column_widths:return
        rows=[(key,self.get_row(key)) for key in self.rows]
        selected=self.cursor_row
        scroll=self.scroll_offset
        self.clear(columns=True)
        for index,((label,_,_),width) in enumerate(zip(specs,widths)):
            self.add_column(label,width=width,key=str(index))
        for key,values in rows:self.add_row(*values,key=key)
        self.column_widths=widths
        if rows:self.move_cursor(row=min(selected,len(rows)-1),scroll=False)
        self.scroll_to(x=0,y=scroll.y,animate=False)


class ObservatoryApp(App):
    TITLE='Agent Observatory'
    AUTO_FOCUS='#agent-table'
    CSS='''
    Screen { background: #0d1117; color: #dce4ed; }
    * { scrollbar-color: #405566; scrollbar-color-hover: #6b8995; scrollbar-color-active: #6ee7c2; scrollbar-background: #101720; scrollbar-background-hover: #101720; scrollbar-background-active: #101720; }
    #masthead { height: 2; padding: 0 2; align-vertical: middle; }
    #brand { width: 1fr; color: #e6edf3; text-style: bold; text-wrap: nowrap; }
    #connection { width: auto; color: #6ee7c2; text-align: right; text-wrap: nowrap; }
    #summary { height: 3; margin: 0 2 1 2; }
    .stat { width: 1fr; border-left: solid #303b4b; padding: 0 2; text-wrap: nowrap; text-overflow: ellipsis; }
    #stat-sessions { border-left: solid #6ee7c2; }
    #controls { height: 3; margin: 0 2; align-vertical: middle; }
    #scope-label { width: 7; height: 1; margin-top: 1; color: #8b9bb0; }
    #scope { width: 37; margin-right: 1; }
    SelectCurrent { background: #161e29; border: tall #263345; }
    Select:focus > SelectCurrent { border: tall #6ee7c2; background-tint: transparent; }
    SelectOverlay { background: #161e29; border: tall #6ee7c2; }
    #controls-spacer { width: 1fr; }
    #reviews { width: 14; border: none; background: #0d1117; margin-top: 1; color: #8b9bb0; }
    Checkbox > .toggle--button { color: #56667b; background: #161e29; }
    Checkbox.-on > .toggle--button { color: #6ee7c2; }
    Button { height: 3; min-width: 9; background: #161e29; color: #b5c6d9; border: tall #263345; text-style: none; }
    Button:hover { background: #213345; border: tall #6ee7c2; }
    Button:focus { border: tall #6ee7c2; text-style: bold; }
    Button:disabled { color: #56667b; border: tall #1d2734; background: #101720; }
    #pause { width: 12; }
    #pages { height: 1fr; margin: 0 2; }
    Tabs { height: 3; padding-top: 1; }
    Tab { color: #8b9bb0; padding: 0 2; }
    Tab.-active { color: #6ee7c2; text-style: bold; }
    Tabs:focus .-active { background: #183a35; color: #9ff3d9; }
    Underline > .underline--bar { color: #6ee7c2; background: #263345; }
    TabPane { padding: 0; }
    ContentSwitcher { height: 1fr; }
    .hint { height: 2; color: #8b9bb0; }
    .box { border: round #303b4b; border-title-color: #b5c6d9; border-title-style: bold; background: #101720; padding: 0 1; }
    .box:focus-within { border: round #537784; border-title-color: #6ee7c2; }
    .results { height: 1fr; }
    .searchbar { height: 3; align-vertical: middle; }
    .view-search { width: 1fr; margin-right: 1; padding: 0 1; background: #161e29; border: tall #263345; }
    .view-search:focus { border: tall #6ee7c2; background-tint: transparent; }
    Input > .input--placeholder { color: #8b9bb0; }
    Input > .input--cursor { background: #6ee7c2; color: #0d1117; }
    .clear-search { width: 9; }
    .error-filter { width: 12; margin-right: 1; }
    .error-filter.-on { color: #ff8b92; border: tall #ff8b92; background: #2d2028; }
    .search-status { height: 1; color: #8b9bb0; padding: 0 1; }
    DataTable { background: #101720; height: 1fr; scrollbar-size: 1 1; }
    DataTable:focus { background-tint: transparent; }
    DataTable > .datatable--header { background: #1a2533; color: #a4b7cb; text-style: bold; }
    DataTable > .datatable--cursor { background: #203d47; color: #eefdf8; text-style: bold; }
    DataTable:blur > .datatable--cursor { background: #1d2d3b; color: #dce4ed; text-style: none; }
    DataTable > .datatable--hover { background: #1a2b38; }
    DataTable > .datatable--odd-row { background: #131d29; }
    DataTable > .datatable--even-row { background: #101720; }
    RichLog { background: #101720; color: #dce4ed; scrollbar-size: 1 1; }
    RichLog:focus { background-tint: transparent; }
    #agent-table { height: 1fr; min-height: 3; }
    #agent-bottom { height: 14; margin-top: 1; }
    #agent-info { width: 1fr; margin-right: 1; }
    #agent-detail { width: 100%; height: 1fr; }
    #agent-open { height: 3; width: 100%; }
    #agent-actions { width: 1fr; }
    .split { height: 1fr; }
    .list { width: 3fr; margin-right: 1; }
    .detail { width: 2fr; }
    .detail-log { height: 1fr; }
    .detail-button { height: 3; width: 100%; }
    .inspector-actions { height: 3; }
    .inspector-actions Button { width: 1fr; }
    .expand-button { min-width: 12; }
    #trace-detail { width: 2fr; }
    #span-tree { height: 1fr; min-height: 4; background: #101720; scrollbar-size: 1 1; }
    #span-info { height: 10; border-top: solid #303b4b; }
    #health { height: 1; padding: 0 2; color: #8b9bb0; }
    Footer { background: #161e29; color: #8b9bb0; }
    FooterKey { background: #20313e; color: #6ee7c2; }
    FooterKey > .footer-key--key { background: #20313e; color: #6ee7c2; }
    FooterKey > .footer-key--description { background: #161e29; color: #b5c6d9; }
    FooterLabel { background: #161e29; color: #b5c6d9; }
    .narrow #masthead { padding: 0 1; }
    .narrow #summary { margin: 0 1; }
    .narrow .stat { padding: 0 1; }
    .narrow #controls { margin: 0 1; }
    .narrow #scope { width: 29; }
    .narrow #pages { margin: 0 1; }
    .narrow Tab { padding: 0 1; }
    .narrow #agent-bottom { height: 12; }
    .narrow .split { layout: vertical; }
    .narrow .list { width: 100%; height: 1fr; margin-right: 0; margin-bottom: 1; }
    .narrow .detail, .narrow #trace-detail { width: 100%; height: 1fr; }
    .compact #masthead { height: 1; }
    .compact #summary { margin-bottom: 0; }
    .compact Tabs { height: 2; padding-top: 0; }
    .compact #agent-bottom, .compact .detail, .compact #trace-detail, .compact #host-detail { display: none; }
    .compact .list { width: 100%; height: 1fr; margin: 0; }
    .expanded .results { display: none; }
    .expanded #agent-bottom { display: block; height: 1fr; margin: 0; }
    .expanded .detail, .expanded #trace-detail, .expanded #host-detail { display: block; width: 100%; height: 1fr; }
    .expanded #agent-info { width: 100%; margin: 0; }
    .expanded #agent-actions { display: none; }
    .expanded #span-info { height: 2fr; }
    .inspecting #summary, .inspecting #controls { display: none; }
    '''
    BINDINGS=[Binding('/','search','Search'),Binding('e','expand','Expand'),Binding('t','open_trace','Trace'),
              Binding('p','pause','Pause'),Binding('a','all','Reset',show=False),Binding('v','reviews','Reviews',show=False),
              Binding('b','browser','Web'),Binding('q','quit','Quit'),Binding('escape','back','Back',show=False),
              Binding('s','focus_agent','Focus agent',show=False),Binding('1','page("agents")','Agents',show=False),
              Binding('2','page("activity")','Activity',show=False),Binding('3','page("mcp")','MCP',show=False),
              Binding('4','page("traces")','Traces',show=False),Binding('5','page("host")','Host',show=False),Binding('6','page("output")','Output',show=False)]

    def __init__(self,feed,agent=None,owns_receiver=False):
        super().__init__()
        self.feed,self.agent,self.owns_receiver=feed,agent,owns_receiver
        self.data=feed.snapshot
        self.last_update=0
        self.table_data={}
        self.table_signatures={}
        self.inspector_signatures={}
        self.selected_source=None
        self.selected_activity=None
        self.selected_mcp=None
        self.scope_options=None
        self.scope_value='all'
        self.span_signature=None
        self.search_timers={}
        self.refresh_timer=None
        self.search_queries={page:'' for page in ('agents','activity','mcp','traces','host')}
        self.errors_only=set()

    def results(self,page,table,placeholder,classes=''):
        """Keep a view's filter, result count, and rows in one panel."""
        controls=[Input(placeholder=placeholder,id='search-'+page,classes='view-search')]
        if page in {'activity','mcp'}:
            controls.append(Button('Errors',id='errors-'+page,classes='error-filter',tooltip='Show only failed calls in the current results'))
        controls.append(Button('Clear',id='clear-'+page,classes='clear-search',disabled=True))
        return Vertical(
            Horizontal(*controls,classes='searchbar'),
            Static('Loading…',id='mcp-hint' if page=='mcp' else 'search-status-'+page,classes='search-status',markup=False),
            ResultsTable(id=table,cursor_type='row',zebra_stripes=True),
            id='results-'+page,classes='box results '+classes)

    def compose(self)->ComposeResult:
        with Horizontal(id='masthead'):
            yield Static('◈  OBSERVATORY',id='brand')
            yield Static('○ Connecting',id='connection',markup=False)
        with Horizontal(id='summary'):
            for name in ('sessions','processes','mcp-count','usage'):
                yield Static('',id='stat-'+name,classes='stat',markup=False)
        with Horizontal(id='controls'):
            yield Static('Scope',id='scope-label')
            yield Select([('All agent sessions','all')],value='all',allow_blank=False,id='scope')
            yield Static('',id='controls-spacer')
            yield Checkbox('Reviews',id='reviews')
            yield Button('Pause',id='pause')
        with TabbedContent(initial='agents',id='pages'):
            with TabPane('1  Agents',id='agents'):
                yield self.results('agents','agent-table','Filter agents by task, project, model, or session…')
                with Horizontal(id='agent-bottom'):
                    with Vertical(id='agent-info'):
                        yield RichLog(id='agent-detail',classes='box',min_width=1,wrap=True,markup=False,auto_scroll=False)
                        yield Button(Text('Session activity  ·  s'),id='agent-open')
                    yield ResultsTable(id='agent-actions',classes='box',cursor_type='row',zebra_stripes=True)
            with TabPane('2  Activity',id='activity'):
                with Horizontal(classes='split'):
                    yield self.results('activity','activity-table','Filter activity by command, message, tool, or path…','list')
                    with Vertical(id='activity-inspector',classes='detail box'):
                        yield RichLog(id='activity-detail',classes='detail-log',min_width=1,wrap=True,markup=False,auto_scroll=False)
                        with Horizontal(classes='inspector-actions'):
                            yield Button(Text('Open trace · t'),id='activity-trace')
                            yield Button(Text('Expand · e'),id='expand-activity',classes='expand-button')
            with TabPane('3  MCP Calls',id='mcp'):
                with Horizontal(classes='split'):
                    yield self.results('mcp','mcp-table','Filter MCP calls by server, tool, arguments, or result…','list')
                    with Vertical(id='mcp-inspector',classes='detail box'):
                        yield RichLog(id='mcp-detail',classes='detail-log',min_width=1,wrap=True,markup=False,auto_scroll=False)
                        with Horizontal(classes='inspector-actions'):
                            yield Button(Text('Open trace · t'),id='mcp-trace')
                            yield Button(Text('Expand · e'),id='expand-mcp',classes='expand-button')
            with TabPane('4  Traces',id='traces'):
                with Horizontal(classes='split'):
                    yield self.results('traces','trace-table','Filter traces by task, model, service, or ID…','list')
                    with Vertical(id='trace-detail',classes='box'):
                        yield Tree('Select a trace',id='span-tree')
                        yield RichLog(id='span-info',min_width=1,wrap=True,markup=False,auto_scroll=False)
            with TabPane('5  Host',id='host'):
                yield self.results('host','host-table','Filter processes by name, PID, project, or state…')
                yield RichLog(id='host-detail',classes='box',min_width=1,wrap=True,markup=False,auto_scroll=False)
            if self.agent:
                with TabPane('6  Output',id='output'):
                    yield Static('stdout / stderr from the agent launched by this cockpit.',id='output-state',classes='hint')
                    yield RichLog(id='agent-output',classes='box',min_width=1,wrap=True,markup=False,auto_scroll=True)
        yield Static('Connecting to receiver…',id='health',markup=False)
        yield Footer()

    def on_mount(self):
        columns={
            'agent-table':[('Agent',15,1),('Project',10,1),('Status',8,0),('Current / last task',20,5),('Seen',7,0)],
            'agent-actions':[('Time',8,0),('Type',7,0),('Recent actions',15,1)],
            'activity-table':[('Time',8,0),('Type',7,0),('Result',9,0),('Action',20,1)],
            'mcp-table':[('Time',8,0),('Agent',11,1),('Server',9,1),('Tool',14,3),('Result',7,0),('Latency',7,0)],
            'trace-table':[('Result',11,0),('Task / run',20,1),('Spans',5,0),('Tokens in / out',15,0)],
            'host-table':[('Process',10,1),('PID',7,0),('Project',12,2),('State',8,1),('RAM',9,0),('CPU total',9,0)]}
        for name,cols in columns.items():
            self.query_one('#'+name,ResultsTable).configure(cols)
        for name,title in [('results-agents','Agent sessions'),('agent-detail','Session overview'),('agent-actions','Recent work'),('results-activity','Activity stream'),('activity-inspector','Event detail'),('results-mcp','MCP calls'),('mcp-inspector','Call detail'),('results-traces','Traces'),('trace-detail','Execution tree'),('results-host','Agent processes'),('host-detail','Collection status')]:
            self.query_one('#'+name).border_title=title
        for page in self.search_queries:
            self.query_one('#results-'+page).border_subtitle='↑↓ select  ·  Enter inspect'
            self.query_one('#search-'+page).tooltip='Search this view · / to focus · Esc to return'
        self.query_one('#reviews').tooltip='Include internal approval review sessions'
        self.query_one('#pause').tooltip='Pause display updates · collection continues'
        self.query_one('#host-detail').styles.height=8
        self.update_layout()
        if not self.feed.thread.is_alive():self.feed.thread.start()
        self.refresh_timer=self.set_interval(.3,self.refresh_data)
        self.query_one('#agent-table',DataTable).focus()

    def on_unmount(self):
        if self.refresh_timer:self.refresh_timer.stop()
        for timer in self.search_timers.values():timer.stop()

    def on_resize(self,event):
        self.update_layout(event.size)
        self.call_after_refresh(self.reflow_inspectors)

    def reflow_inspectors(self):
        self.inspector_signatures.clear()
        self.last_update=-1
        self.refresh_data()
        trees=self.query('#span-tree')
        if trees and trees.first().cursor_node:
            self.show_span(trees.first().cursor_node.data)

    def update_layout(self,size=None):
        size=size or self.size
        self.screen.set_class(size.width<140,'narrow')
        self.screen.set_class(size.height<36,'compact')

    def stat(self,name,value,label,detail):
        text=Text(label.upper()+'\n',style='#8b9bb0',no_wrap=True,overflow='ellipsis')
        text.append(str(value),style='bold #e6edf3')
        text.append('\n'+detail,style='#8b9bb0')
        self.query_one('#stat-'+name,Static).update(text)

    def fill(self,name,rows,preserve=None):
        """Preserve row identity and scroll position while live values change."""
        signature=tuple((key,tuple(str(value) for value in values)) for key,values,record in rows)
        self.table_data[name]={key:record for key,values,record in rows}
        if self.table_signatures.get(name)==signature:return
        self.table_signatures[name]=signature
        table=self.query_one('#'+name,DataTable)
        selected=preserve
        if table.row_count:
            try:
                if not selected:selected=table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            except Exception:pass
        scroll=table.scroll_offset
        table.clear()
        for key,values,record in rows:table.add_row(*values,key=key)
        if rows:
            index=next((index for index,(key,_,__) in enumerate(rows) if key==selected),0)
            table.move_cursor(row=index,scroll=False)
            table.scroll_to(x=scroll.x,y=scroll.y,animate=False)

    def search_status(self,page,count,noun,plural=None):
        query=self.search_queries[page]
        waiting=page in {'activity','mcp','traces'} and self.data.queries.get(page,'')!=query
        text='Filtering…' if waiting else quantity(count,('matching '+noun) if query else noun,
                                                    ('matching '+plural) if query and plural else plural)
        if not waiting and query and not count:text+=' · Clear to remove this filter'
        if page in self.errors_only:text+=' · errors only'
        self.query_one('#search-status-'+page,Static).update(text)

    def visible_results(self,page,items):
        if self.data.queries.get(page,'')!=self.search_queries[page]:return []
        if page in self.errors_only:
            return [item for item in items if str(item.get('status') or item.get('result') or '').lower() in {'error','failed','denied','blocked','policy_denied','incomplete'}]
        return items

    def refresh_data(self):
        # Timer messages can already be queued when child widgets unmount.
        if not self.query('#health'):return
        data=self.feed.snapshot
        connection='Ⅱ Paused' if self.feed.paused else '○ Reconnecting' if data.error else '● Connected'
        endpoint=self.feed.api.endpoint.removeprefix('http://').removeprefix('https://')
        self.query_one('#connection',Static).update(Text(connection+'  '+endpoint,
            style='#f2c779' if self.feed.paused else '#ff8b92' if data.error else '#6ee7c2'))
        health=Text(data.error,style='#ff8b92') if data.error else Text(
            f"{'Display paused' if self.feed.paused else 'Updated '+age(int(data.updated*1e9))}  ·  {self.scope_label()}  ·  {'Reviews included' if self.feed.include_reviews else 'Reviews hidden'}",
            style='#8b9bb0')
        self.query_one('#health',Static).update(health)
        if data.updated==self.last_update:return
        self.last_update=data.updated;self.data=data
        if self.agent:
            lines=list(self.agent.lines)
            if getattr(self,'output_lines',None)!=lines:
                self.output_lines=lines
                output=self.query_one('#agent-output',RichLog);output.clear();output.write(Text('\n'.join(lines)))
            code=self.agent.process.poll()
            self.query_one('#output-state',Static).update('Agent process running · stdout / stderr' if code is None else f'Agent process exited with code {code} · captured stdout / stderr')
        agents=sources(data,self.feed.include_reviews)
        work=sources(data,False)
        recent=sum(item['recent'] for item in work)
        reviews=sum(entry.get('service')=='codex-review' for entry in data.local_status.get('sessions',[]))
        processes=(data.local_status.get('runtime') or {}).get('processes',[])
        live=sum(not str(item.get('state','')).startswith(('Z','X')) for item in processes)
        self.stat('sessions',len(work),'Agent sessions',f'{recent} recent · {len(work)-recent} idle / offline')
        self.stat('processes',live,'Host processes','PIDs on this host')
        servers={item.get('server') for item in data.mcp if item.get('server') and item.get('server')!='Not reported'}
        operations={(item.get('server'),item.get('tool')) for item in data.mcp if item.get('tool') and item.get('tool')!='Not reported'}
        mcp_errors=sum(str(item.get('status','')).upper() in {'ERROR','FAILED'} for item in data.mcp)
        self.stat('mcp-count',quantity(len(data.mcp),'call')+('' if self.size.width<110 else ' · '+quantity(len(servers),'server')),'MCP in scope',
                  f'{quantity(len(servers),"server")} · {quantity(mcp_errors,"error")}' if self.size.width<110 else f'{quantity(len(operations),"tool")} · {quantity(mcp_errors,"error")}')
        self.stat('usage',number(data.totals.get('input_tokens'))+' / '+number(data.totals.get('output_tokens')),'Tokens in / out','Includes cached input')
        self.query_one('#stat-sessions').tooltip=f'{reviews} approval reviews · Recent means a session record changed within 35 seconds'
        self.query_one('#stat-usage').tooltip='Recorded token totals; repeated and cached input is included. This is not a billable cost estimate.'
        options=[('All agent sessions','all')]+[(f"{item['runtime']} · {item.get('project') or 'SDK'} · {(item.get('session_id') or item['id'])[-6:]}",item['id']) for item in agents]
        if options!=self.scope_options:
            picker=self.query_one('#scope',Select);old=self.scope_value
            picker.set_options(options);picker.value=old if any(value==old for _,value in options) else 'all';self.scope_options=options
        self.source_map={item['id']:item for item in agents}
        agent_query=self.search_queries['agents'].casefold()
        visible_agents=[item for item in agents if (self.scope_value=='all' or item['id']==self.scope_value) and
                        (not agent_query or agent_query in ' '.join(str(item.get(key) or '') for key in ('runtime','service','project','task','model','session_id','id','state')).casefold())]
        self.fill('agent-table',[(item['id'],[cell(item['runtime']+' · '+(item.get('session_id') or item['id'])[-6:]),cell(item.get('project') or '—','#a4b7cb'),cell(('● ' if item['recent'] else '○ ')+item['state'],'#6ee7c2' if item['recent'] else '#8b9bb0'),cell(item.get('task') or 'Task not reported'),cell(age(item['stamp']),'#8b9bb0')],item) for item in visible_agents])
        self.search_status('agents',len(visible_agents),'session')
        if self.selected_source not in {item['id'] for item in visible_agents}:self.selected_source=visible_agents[0]['id'] if visible_agents else None
        self.show_agent()
        activity_items=self.visible_results('activity',data.activity_results if data.activity_results is not None else data.activity)
        activity=[(item['source_id'],[cell(time.strftime('%H:%M:%S',time.localtime(item['time_unix_nano']/1e9))),cell(item['domain'].upper(),'#92b9eb'),status(item.get('result')),cell(headline(item))],item) for item in activity_items]
        self.fill('activity-table',activity)
        self.search_status('activity',len(activity),'event')
        if self.selected_activity not in self.table_data.get('activity-table',{}):self.selected_activity=activity[0][0] if activity else None
        self.show_activity()
        mcp_items=self.visible_results('mcp',data.mcp_results if data.mcp_results is not None else data.mcp)
        self.fill('mcp-table',[(item['id'],[cell(table_when(item['time_unix_nano']),'#8b9bb0'),cell((item.get('service') or 'Unknown')+' · '+str(item.get('session_id') or '—')[-6:]),cell(item['server'],'#a4b7cb'),cell(item['tool']),status(item['status']),cell('—' if item['duration_ms'] is None else timing(item['duration_ms']))],item) for item in mcp_items])
        if self.selected_mcp not in self.table_data.get('mcp-table',{}):self.selected_mcp=mcp_items[0]['id'] if mcp_items else None
        self.show_mcp()
        if not data.mcp_available:
            mcp_hint='This receiver version does not expose MCP operations. Update and restart the receiver.'
        elif data.queries.get('mcp','')!=self.search_queries['mcp']:
            mcp_hint='Filtering MCP operations…'
        elif mcp_items:
            server_count=len({item.get('server') for item in mcp_items})
            error_count=sum(str(item.get('status','')).upper() in {'ERROR','FAILED'} for item in mcp_items)
            mcp_hint=f"{quantity(len(mcp_items),'matching operation' if self.search_queries['mcp'] else 'operation')} · {quantity(server_count,'server')} · {quantity(error_count,'error')}"
        elif self.search_queries['mcp']:
            mcp_hint='No matching MCP operations · Clear to show all calls in this scope'
        else:
            mcp_hint='No MCP operations in this scope · General tool activity is available in Activity'
        if 'mcp' in self.errors_only:mcp_hint=f"{quantity(len(mcp_items),'failed call')} · errors only · current results"
        self.query_one('#mcp-hint',Static).update(mcp_hint)
        traces=self.visible_results('traces',data.traces[:])
        detail=data.detail
        if self.feed.pinned_trace and detail.get('summary') and not any(item['trace_id']==detail['trace_id'] for item in traces):traces.insert(0,detail['summary'])
        self.fill('trace-table',[(item['trace_id'],[cell('Active · errors','yellow') if item.get('active') and item['status']=='ERROR' else cell('Error','red') if item['status']=='ERROR' else status(item['status']),cell(trace_name(item)),cell(item['span_count']),cell(number(item['input_tokens'])+' / '+number(item['output_tokens']))],item) for item in traces],preserve=self.feed.selected_trace)
        self.search_status('traces',len(traces),'trace')
        self.show_spans()
        host_query=self.search_queries['host'].casefold()
        visible_processes=[item for item in processes if not host_query or host_query in ' '.join(str(item.get(key) or '') for key in ('name','pid','parent_pid','project','state')).casefold()]
        self.fill('host-table',[(str(item['pid']),[cell(item['name']),cell(item['pid']),cell(item.get('project') or '—'),cell(item['state']),cell(f"{item['memory_rss_kib']/1024:.0f} MiB"),cell(f"{item['cpu_seconds']:.1f} s")],item) for item in visible_processes])
        self.search_status('host',len(visible_processes),'process','processes')
        runtime=data.local_status.get('runtime') or {}
        self.inspect('host-detail','Host & collection status',[
            ('Host',runtime.get('host','Not reported')),('Platform',str(runtime.get('os',''))+' / '+str(runtime.get('architecture',''))),
            ('Load average',', '.join(str(value) for value in runtime.get('load_average',[])) or 'Unavailable'),
            ('Sources',f"{data.local_status.get('sources',0)} recent files · lookback {data.local_status.get('lookback_hours',24)}h · read errors {data.local_status.get('read_errors',0)}"),
            ('Coverage','Source logs can be partial. Costs and model-call latency are unavailable unless reported. CPU is cumulative process time.')],[])

    def scope_label(self):
        item=getattr(self,'source_map',{}).get(self.scope_value)
        return f"{item['runtime']} / {item.get('project') or 'SDK'}" if item else 'all agent sessions'

    def inspect(self,name,title,fields,sections):
        signature=(title,str(fields),str(sections))
        if self.inspector_signatures.get(name)==signature:return
        previous=self.inspector_signatures.get(name)
        self.inspector_signatures[name]=signature
        log=self.query_one('#'+name,RichLog)
        scroll=log.scroll_offset
        log.clear();log.write(Text(title,style='bold #e6edf3'))
        table=Table.grid(padding=(0,2));table.add_column(style='#8b9bb0',no_wrap=True);table.add_column()
        for label,value in fields:table.add_row(Text(label),Text(str('Not reported' if value is None or value=='' else value)))
        log.write(table)
        for label,value in sections:self.write_evidence(log,label,value)
        if previous and previous[0]==title and previous[2]==signature[2]:
            log.scroll_to(y=scroll.y,animate=False)

    def write_evidence(self,log,label,value,depth=0):
        if value is None or value=='':return
        value=decode_nested(value)
        # Native log adapters may wrap each piece of the same record in a
        # one-key `recorded_content` envelope. Unwrap that without consuming
        # the JSON nesting budget or repeating the label at every level.
        if isinstance(value,dict) and len(value)==1:
            key,child=next(iter(value.items()))
            if key.lower() in {'recorded_content','content','payload'}:
                self.write_evidence(log,label,child,depth);return
            if key.lower()=='wrapper':
                self.write_evidence(log,label,child,depth+1);return
        if depth==0 and isinstance(value,(dict,list)) and value:
            log.write(Text('\n'+label.upper(),style='bold #6ee7c2'))
            if label.lower()=='arguments':
                write_json(log,value)
                return
        if isinstance(value,(dict,list)) and not value:
            log.write(Text('\n'+label.title(),style='bold #92b9eb'))
            log.write(Text('No entries' if isinstance(value,list) else 'Empty object'))
            return
        if isinstance(value,dict) and value.get('type') in {'text','input_text','output_text'} and 'text' in value:
            self.write_evidence(log,label,value['text'],depth+1);return
        # Any structured value nested in captured evidence is easier to scan
        # as JSON than as a sequence of flattened label/value rows. This also
        # handles JSON strings stored under fields such as `output` or `text`.
        if depth and isinstance(value,(dict,list)):
            write_json(log,value)
            return
        if depth<64 and isinstance(value,dict):
            for key,child in value.items():
                self.write_evidence(log,str(key).replace('_',' '),child,depth+1)
            return
        if depth<64 and isinstance(value,list):
            for child in value:self.write_evidence(log,label,child,depth+1)
            return
        if isinstance(value,(dict,list)):
            log.write(Text('\n'+label.title(),style='bold #92b9eb'))
            write_json(log,value)
            return
        log.write(Text('\n'+label.title(),style='bold #92b9eb'))
        if label.lower()=='command':
            command=shell_command(value)
            script=heredoc_script(command)
            if script:
                language,title,body=script
                log.write(Text(title,style='bold #92b9eb'))
                log.write(Syntax(body or '# Empty script',language,word_wrap=True,background_color='#101720'))
                return
            log.write(Syntax(command,'bash',word_wrap=True,background_color='#101720'))
        elif label.lower() in {'diff','patch','unified diff','code'}:
            lexer='diff' if 'diff' in label.lower() or label.lower()=='patch' else 'python' if label.lower()=='code' else 'bash'
            log.write(Syntax(str(value),lexer,word_wrap=True,background_color='#101720'))
        else:log.write(Text(str(value)))

    def show_agent(self):
        item=getattr(self,'source_map',{}).get(self.selected_source)
        if not item:
            self.query_one('#agent-open',Button).disabled=True
            self.fill('agent-actions',[])
            if self.search_queries['agents']:
                self.inspect('agent-detail','No matching agent sessions',[],[('Search','Clear the filter in Agent sessions to show all sessions in this scope.')])
            else:
                self.inspect('agent-detail','No agent sessions found',[],[('Next step','Local Codex and Claude Code sessions are discovered automatically. Python SDK agents can use the capture launcher.')])
            return
        self.query_one('#agent-open',Button).disabled=False
        summary=next((entry for entry in self.data.sessions if item.get('session_id') and entry.get('session_id')==item.get('session_id')),{})
        latest=item.get('last_activity') or {}
        self.inspect('agent-detail',clean(item.get('task') or 'Task not reported'),[
            ('Agent',f"{item['runtime']} · {item.get('project') or 'SDK'}"),
            ('Latest',headline(latest) if latest else 'Not reported'),
            ('Model',item.get('model') or 'Not reported'),('Seen',f"{item['state']} · {age(item.get('stamp'))}"),
            ('Work',f"{summary.get('trace_count',0)} traces · {item.get('spans',0)} spans · {number(summary.get('input_tokens'))} in / {number(summary.get('output_tokens'))} out"),
            ('Cost','Not reported' if not summary or summary.get('unknown_cost_runs') else f"${summary.get('known_cost_usd',0):.4f} known")],
            [('Session ID',item.get('session_id') or item['id'])])
        actions=[event for event in self.data.activity if event.get('session_id')==item.get('session_id')] if item.get('session_id') else [event for event in self.data.activity if event.get('service')==item.get('service')]
        meaningful=[event for event in actions if event.get('domain') in {'shell','file','mcp','browser','model','message'}]
        actions=sorted(meaningful or actions,key=lambda event:event.get('time_unix_nano',0),reverse=True)
        self.fill('agent-actions',[(event['source_id'],[cell(time.strftime('%H:%M:%S',time.localtime(event['time_unix_nano']/1e9))),cell(event['domain'].upper()),cell(headline(event))],event) for event in actions[:30]])

    def show_activity(self):
        item=self.table_data.get('activity-table',{}).get(self.selected_activity)
        self.query_one('#activity-trace',Button).disabled=not bool(item and item.get('trace_id'))
        if not item:
            if 'activity' in self.errors_only:
                self.inspect('activity-detail','No failed events',[],[('Errors filter','No failures in the current results. Turn off Errors to see all activity.')]);return
            self.inspect('activity-detail','No activity in this scope',[],[('Scope','Choose another agent session or clear the search to view recent activity.')]);return
        details=decode(item.get('detail') or '')
        sections=list(details.items()) if isinstance(details,dict) else [('Recorded content',details)]
        self.inspect('activity-detail',headline(item),[('Agent',item.get('service')),('Type',item.get('domain')),('Result',str(status(item.get('result')))),
            ('When',when(item['time_unix_nano'])),('Duration',timing(item.get('duration_ms')))],
            sections+[('Source',item.get('source')),('Trace ID',item.get('trace_id')),('Span ID',item.get('span_id'))])

    def show_mcp(self):
        item=self.table_data.get('mcp-table',{}).get(self.selected_mcp)
        self.query_one('#mcp-trace',Button).disabled=not bool(item and item.get('trace_id'))
        if not item:
            if not self.data.mcp_available:
                title='MCP inventory unavailable'
                note='The connected receiver is too old to provide this endpoint. Update and restart the receiver.'
            elif self.data.queries.get('mcp','')!=self.search_queries['mcp']:
                title='Filtering MCP operations'
                note='Waiting for matching calls from the receiver.'
            elif 'mcp' in self.errors_only:
                title='No failed MCP calls'
                note='No failures in the current results. Turn off Errors to see all calls.'
            elif self.search_queries['mcp']:
                title='No matching MCP operations'
                note='Clear Search or choose another agent session. Search also matches server, tool, arguments, and result.'
            else:
                title='No MCP operations in this scope'
                note='Calls identified as Model Context Protocol appear here. Other tool activity remains in Activity.'
            self.inspect('mcp-detail',title,[('Scope',self.scope_label())],[('How to proceed',note)])
            return
        self.inspect('mcp-detail',item['server']+' / '+item['tool'],[
            ('Result',str(status(item.get('status')))+' · '+timing(item.get('duration_ms'))),
            ('Agent',(item.get('service') or 'Not reported')+' · '+str(item.get('session_id') or 'Unknown session')[-12:]),
            ('Method',item.get('method') or 'Not reported'),('Observed',when(item.get('time_unix_nano') or 0))],
            [('Arguments',item.get('arguments')),('Response',item.get('output')),
             ('Evidence',item.get('source')),('Session ID',item.get('session_id')),
             ('Trace ID',item.get('trace_id') or 'Not linked'),('Span ID',item.get('span_id') or 'Not linked')])

    def show_spans(self):
        detail=self.data.detail
        if not detail.get('trace_id'):
            tree=self.query_one('#span-tree',Tree);tree.clear();tree.root.set_label('No trace selected')
            self.query_one('#span-info',RichLog).clear();self.span_signature=None
            return
        if detail.get('trace_id')!=self.feed.selected_trace:return
        spans=detail.get('spans',[])
        signature=(detail.get('trace_id'),tuple((item['span_id'],item['status'],item['end_unix_nano'],str(item['attributes'])) for item in spans))
        if signature==self.span_signature:return
        self.span_signature=signature
        tree=self.query_one('#span-tree',Tree)
        selected=tree.cursor_node.data.get('span_id') if tree.cursor_node and tree.cursor_node.data else None
        tree.clear()
        tree.root.set_label(Text('Trace '+str(detail.get('trace_id',''))[:12],style='bold'))
        nodes={};pending=list(spans)
        for _ in range(len(spans)+1):
            remaining=[]
            for item in pending:
                parent=item.get('parent_span_id')
                if parent and parent not in nodes and any(span['span_id']==parent for span in spans):remaining.append(item);continue
                attrs=item['attributes'];kind='MCP' if attrs.get('mcp.server.name') or attrs.get('mcp.tool.name') else 'MODEL' if attrs.get('gen_ai.operation.name')=='chat' or 'gen_ai.usage.input_tokens' in attrs else 'TOOL' if attrs.get('tool.name') else 'RUN'
                duration=None if attrs.get('observatory.duration_unknown') else (item['end_unix_nano']-item['start_unix_nano'])/1e6
                label=Text(f"{kind:5} ",style='#92b9eb');label.append(clean(item['name']));label.append(' · '+timing(duration),style='dim')
                if item['status']=='ERROR':label.append(' · ERROR',style='bold red')
                nodes[item['span_id']]=(nodes.get(parent) or tree.root).add(label,data=item,expand=True)
            if not remaining:break
            if len(remaining)==len(pending):
                for item in remaining:nodes[item['span_id']]=tree.root.add(Text(item['name']),data=item)
                break
            pending=remaining
        tree.root.expand()
        self.query_one('#trace-detail').border_subtitle=f"{len(spans)} / {detail.get('span_page',{}).get('trace_total',len(spans))} spans loaded"
        if getattr(self,'jump_span',None) in nodes:
            self.call_after_refresh(tree.move_cursor,nodes[self.jump_span]);self.jump_span=None
        elif nodes:
            self.call_after_refresh(tree.move_cursor,nodes.get(selected) or next(iter(nodes.values())))

    def valid_row(self,event):
        table=event.data_table
        if not table.row_count:return None
        try:
            if table.coordinate_to_cell_key(table.cursor_coordinate).row_key!=event.row_key:return None
        except Exception:return None
        return self.table_data.get(table.id,{}).get(event.row_key.value)

    @on(DataTable.RowHighlighted)
    def row_highlighted(self,event):
        item=self.valid_row(event)
        if not item:return
        name=event.data_table.id
        if name=='agent-table':self.selected_source=item['id'];self.show_agent()
        elif name=='activity-table':self.selected_activity=item['source_id'];self.show_activity()
        elif name=='mcp-table':self.selected_mcp=item['id'];self.show_mcp()
        elif name=='trace-table':
            if self.feed.pinned_trace and self.data.detail.get('trace_id')!=self.feed.selected_trace:return
            if self.feed.selected_trace!=item['trace_id']:
                self.feed.selected_trace=item['trace_id'];self.feed.pinned_trace=False;self.feed.request_refresh()
                self.feed.selected_span_id=None
                self.query_one('#span-tree',Tree).root.set_label(Text('Loading selected trace…'))

    @on(DataTable.RowSelected)
    def row_selected(self,event):
        item=self.valid_row(event)
        if not item:return
        if event.data_table.id!='agent-actions' and self.screen.has_class('compact'):
            self.action_expand()
            return
        if event.data_table.id=='agent-table':
            self.selected_source=item['id'];self.show_agent();self.query_one('#agent-detail',RichLog).focus()
        elif event.data_table.id=='agent-actions':
            self.selected_activity=item['source_id'];self.action_page('activity');self.show_activity()
        elif event.data_table.id=='activity-table':self.query_one('#activity-detail',RichLog).focus()
        elif event.data_table.id=='mcp-table':self.query_one('#mcp-detail',RichLog).focus()
        elif event.data_table.id=='trace-table':self.query_one('#span-tree',Tree).focus()
        elif event.data_table.id=='host-table':self.query_one('#host-detail',RichLog).focus()

    @on(Tree.NodeHighlighted,'#span-tree')
    def span_highlighted(self,event):
        self.show_span(event.node.data)

    def show_span(self,span):
        if not span:return
        attrs=span['attributes']
        self.inspect('span-info',span['name'],[('Span',span['span_id']),('Parent',span.get('parent_span_id') or 'Root'),
            ('Status',span['status']),('Service',span['service'])],[(key,value) for key,value in attrs.items()])

    @on(Select.Changed,'#scope')
    def scope_changed(self,event):
        if event.value==Select.BLANK or event.value==self.scope_value:return
        self.scope_value=str(event.value);item=getattr(self,'source_map',{}).get(self.scope_value)
        self.feed.session_id=item.get('session_id') or '' if item else ''
        self.feed.service=item.get('service') or '' if item else ''
        self.feed.selected_trace=None;self.feed.pinned_trace=False;self.feed.request_refresh()
        self.feed.selected_span_id=None
        self.last_update=-1;self.refresh_data()

    @on(Input.Changed,'.view-search')
    def search_changed(self,event):
        page=event.input.id.removeprefix('search-')
        value=event.value.strip()
        self.query_one('#clear-'+page,Button).disabled=not event.value
        if self.search_queries[page]==value:return
        self.search_queries[page]=value
        if page in self.search_timers:self.search_timers.pop(page).stop()
        def apply():
            if not self.query('#health'):return
            attr={'activity':'activity_query','mcp':'mcp_query','traces':'query'}.get(page)
            if attr:
                setattr(self.feed,attr,value)
                if page=='traces':
                    self.feed.selected_trace=None;self.feed.selected_span_id=None;self.feed.pinned_trace=False
                self.feed.request_refresh()
            self.last_update=-1;self.refresh_data()
        if page in {'activity','mcp','traces'}:self.search_timers[page]=self.set_timer(.3,apply)
        else:apply()
        self.last_update=-1;self.refresh_data()

    @on(Checkbox.Changed,'#reviews')
    def reviews_changed(self,event):
        self.feed.include_reviews=event.value;self.feed.selected_trace=None;self.feed.pinned_trace=False;self.feed.request_refresh()
        self.feed.selected_span_id=None

    @on(TabbedContent.TabActivated,'#pages')
    def page_activated(self,event):
        self.screen.set_class(event.pane.has_class('expanded'),'inspecting')
        self.call_after_refresh(self.reflow_inspectors)

    @on(Button.Pressed)
    def button_pressed(self,event):
        if event.button.id=='pause':self.action_pause()
        elif event.button.id.startswith('expand-'):self.action_expand()
        elif event.button.id.startswith('errors-'):
            page=event.button.id.removeprefix('errors-')
            if page in self.errors_only:self.errors_only.remove(page)
            else:self.errors_only.add(page)
            event.button.set_class(page in self.errors_only,'-on')
            event.button.label='Errors ●' if page in self.errors_only else 'Errors'
            self.last_update=-1;self.refresh_data()
        elif event.button.id.startswith('clear-'):
            field=self.query_one('#search-'+event.button.id.removeprefix('clear-'),Input)
            field.value='';field.focus()
        elif event.button.id=='agent-open':self.action_focus_agent()
        elif event.button.id in {'activity-trace','mcp-trace'}:self.action_open_trace()

    def action_pause(self):
        self.feed.paused=not self.feed.paused;self.query_one('#pause',Button).label='Resume' if self.feed.paused else 'Pause';self.feed.request_refresh();self.refresh_data()

    def action_search(self):
        page=self.query_one('#pages',TabbedContent).active
        if page in self.search_queries:
            self.collapse(page)
            self.query_one('#search-'+page,Input).focus()
    def action_back(self):
        self.action_page(self.query_one('#pages',TabbedContent).active)
    def collapse(self,page):
        self.query_one('#'+page,TabPane).remove_class('expanded')
        self.screen.remove_class('inspecting')
        for button in self.query('#'+page+' .expand-button'):button.label=Text('Expand · e')
        self.call_after_refresh(self.reflow_inspectors)
    def action_expand(self):
        page=self.query_one('#pages',TabbedContent).active
        if page not in self.search_queries:return
        pane=self.query_one('#'+page,TabPane)
        if pane.has_class('expanded'):
            self.action_back();return
        pane.add_class('expanded')
        self.screen.add_class('inspecting')
        self.call_after_refresh(self.reflow_inspectors)
        for button in self.query('#'+page+' .expand-button'):button.label=Text('Back · e')
        target={'agents':'agent-detail','activity':'activity-detail','mcp':'mcp-detail','traces':'span-tree','host':'host-detail'}[page]
        self.query_one('#'+target).focus()
        self.notify('Expanded detail · Esc to return',timeout=2)
    def action_focus_agent(self):
        if self.selected_source:
            self.query_one('#scope',Select).value=self.selected_source
            self.action_page('activity')
    def action_reviews(self):self.query_one('#reviews',Checkbox).toggle()
    def action_all(self):
        self.query_one('#scope',Select).value='all'
        self.errors_only.clear()
        for button in self.query('.error-filter'):
            button.remove_class('-on');button.label='Errors'
        for page in self.search_queries:self.query_one('#search-'+page,Input).value=''
        self.last_update=-1;self.refresh_data()
    def action_page(self,page):
        if page=='output' and not self.agent:return
        self.collapse(page)
        self.query_one('#pages',TabbedContent).active=page
        target={'agents':'agent-table','activity':'activity-table','mcp':'mcp-table','traces':'trace-table','host':'host-table','output':'agent-output'}[page]
        self.query_one('#'+target).focus()
    def action_browser(self):webbrowser.open(self.feed.api.endpoint)
    def action_open_trace(self):
        page=self.query_one('#pages',TabbedContent).active
        item=self.table_data.get('mcp-table',{}).get(self.selected_mcp) if page=='mcp' else self.table_data.get('activity-table',{}).get(self.selected_activity)
        if not item or not item.get('trace_id'):
            self.notify('This action has no linked trace.');return
        self.jump_span=item.get('span_id')
        self.feed.selected_span_id=self.jump_span
        self.feed.selected_trace=item['trace_id'];self.feed.pinned_trace=True;self.feed.request_refresh();self.action_page('traces')
