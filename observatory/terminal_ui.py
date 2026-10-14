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
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, RichLog, Select, Static, TabbedContent, TabPane, Tree

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
    color='red' if str(value).lower() in {'error','failed','denied','blocked','incomplete'} else 'green' if str(value).lower() in {'success','ok','completed'} else 'cyan' if str(value).lower() in {'running','started'} else 'dim'
    return cell(label,color)


def decode(value):
    for _ in range(3):
        if not isinstance(value,str):
            break
        try:
            parsed=json.loads(value)
        except (ValueError,TypeError):
            break
        if isinstance(parsed,(dict,list)):
            value=parsed
        else:
            break
    return value


def headline(item):
    domain=item.get('domain') or 'tool'
    subject=str(item.get('subject') or item.get('action') or 'Observed activity')
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


class ObservatoryApp(App):
    TITLE='Agent Observatory'
    AUTO_FOCUS='#agent-table'
    CSS='''
    Screen { background: #0c1422; color: #d7e3f3; }
    Header { background: #14243a; color: #d5e7ff; }
    Footer { background: #14243a; }
    #summary { height: 4; margin: 1 2 0 2; }
    .stat { width: 1fr; background: #142238; border-left: thick #3f658f; padding: 0 2; margin-right: 1; }
    #controls { height: 3; margin: 1 2 0 2; }
    #search { width: 1fr; margin-right: 1; }
    #scope { width: 28; margin-right: 1; }
    #reviews { width: 14; border: none; margin-top: 1; }
    #pause { min-width: 9; width: 9; }
    #pages { height: 1fr; margin: 0 2; }
    TabPane { padding: 1 0 0 0; }
    ContentSwitcher { height: 1fr; }
    .hint { height: 2; color: #91a7c3; }
    .box { border: round #2d4665; background: #101e31; padding: 0 1; }
    .box:focus-within { border: round #6c9cd6; }
    DataTable { background: #101e31; height: 1fr; }
    DataTable > .datatable--header { background: #1b304c; color: #9abbe6; text-style: bold; }
    DataTable > .datatable--cursor { background: #294b73; color: white; }
    DataTable > .datatable--odd-row { background: #132238; }
    DataTable > .datatable--even-row { background: #101e31; }
    #agent-table { height: 1fr; min-height: 6; }
    #agent-bottom { height: 14; margin-top: 1; }
    #agent-detail { width: 1fr; margin-right: 1; }
    #agent-info { width: 1fr; margin-right: 1; }
    #agent-info #agent-detail { width: 100%; height: 1fr; margin-right: 0; }
    #agent-open { height: 3; width: 100%; }
    #agent-actions { width: 1fr; }
    .split { height: 1fr; }
    .list { width: 3fr; margin-right: 1; }
    .detail { width: 2fr; }
    .detail-log { height: 1fr; }
    .detail-button { height: 3; width: 100%; }
    #trace-list { width: 1fr; }
    #trace-detail { width: 1fr; }
    #span-tree { height: 1fr; min-height: 5; background: #101e31; }
    #span-info { height: 10; border-top: solid #2d4665; }
    #health { height: 1; padding: 0 2; color: #91a7c3; }
    .narrow #summary { margin: 0 1; }
    .narrow .stat { padding: 0 1; }
    .narrow #controls { margin: 0 1; }
    .narrow #scope { width: 24; }
    .narrow #pages { margin: 0 1; }
    .narrow .list { width: 1fr; }
    .narrow .detail { width: 1fr; }
    .narrow #agent-bottom { height: 10; }
    .narrow .split { layout: vertical; }
    .narrow .list { width: 100%; height: 1fr; margin-right: 0; margin-bottom: 1; }
    .narrow .detail { width: 100%; height: 1fr; }
    .narrow #trace-detail { width: 100%; height: 1fr; }
    '''
    BINDINGS=[Binding('q','quit','Quit'),Binding('p','pause','Pause'),Binding('/','search','Search'),Binding('escape','back','Back',show=False),
              Binding('t','open_trace','Open trace'),Binding('a','all','All sessions'),Binding('v','reviews','Reviews'),
              Binding('b','browser','Web'),Binding('s','focus_agent','Focus agent',show=False),Binding('1','page("agents")','Agents',show=False),
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
        self.search_timer=None

    def compose(self)->ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id='summary'):
            for name in ('sessions','processes','mcp-count','usage'):
                yield Static('',id='stat-'+name,classes='stat',markup=False)
        with Horizontal(id='controls'):
            yield Input(placeholder='Search tasks, commands, models, MCP tools…',id='search')
            yield Select([('All agent sessions','all')],value='all',allow_blank=False,id='scope')
            yield Checkbox('Reviews',id='reviews')
            yield Button('Pause',id='pause')
        with TabbedContent(initial='agents',id='pages'):
            with TabPane('1  Agents',id='agents'):
                yield Static('Agent sessions, current task, model, and recent actions. Select a session to inspect its activity.',classes='hint')
                yield DataTable(id='agent-table',classes='box',cursor_type='row',zebra_stripes=True)
                with Horizontal(id='agent-bottom'):
                    with Vertical(id='agent-info'):
                        yield RichLog(id='agent-detail',classes='box',wrap=True,markup=False,auto_scroll=False)
                        yield Button('Open session activity  [s]',id='agent-open')
                    yield DataTable(id='agent-actions',classes='box',cursor_type='row',zebra_stripes=True)
            with TabPane('2  Activity',id='activity'):
                yield Static('Recorded messages, model calls, commands, file changes, browser actions, and tool results.',classes='hint')
                with Horizontal(classes='split'):
                    yield DataTable(id='activity-table',classes='box list',cursor_type='row',zebra_stripes=True)
                    with Vertical(classes='detail box'):
                        yield RichLog(id='activity-detail',classes='detail-log',wrap=True,markup=False,auto_scroll=False)
                        yield Button('Open related trace  [t]',id='activity-trace',classes='detail-button')
            with TabPane('3  MCP Calls',id='mcp'):
                yield Static('Model Context Protocol operations reported by the selected agents.',id='mcp-hint',classes='hint')
                with Horizontal(classes='split'):
                    yield DataTable(id='mcp-table',classes='box list',cursor_type='row',zebra_stripes=True)
                    with Vertical(classes='detail box'):
                        yield RichLog(id='mcp-detail',classes='detail-log',wrap=True,markup=False,auto_scroll=False)
                        yield Button('Open operation trace  [t]',id='mcp-trace',classes='detail-button')
            with TabPane('4  Traces',id='traces'):
                yield Static('A trace represents one run or turn. Spans record model, tool, and framework operations; select one to inspect its attributes.',classes='hint')
                with Horizontal(classes='split'):
                    yield DataTable(id='trace-table',classes='box list',cursor_type='row',zebra_stripes=True)
                    with Vertical(id='trace-detail',classes='box'):
                        yield Tree('Select a trace',id='span-tree')
                        yield RichLog(id='span-info',wrap=True,markup=False,auto_scroll=False)
            with TabPane('5  Host',id='host'):
                yield Static('Processes observed on this host. Session counts are separate; process-to-session links appear only when the source reports them.',classes='hint')
                yield DataTable(id='host-table',classes='box',cursor_type='row',zebra_stripes=True)
                yield RichLog(id='host-detail',classes='box',wrap=True,markup=False,auto_scroll=False)
            if self.agent:
                with TabPane('6  Output',id='output'):
                    yield Static('stdout / stderr from the agent launched by this cockpit.',id='output-state',classes='hint')
                    yield RichLog(id='agent-output',classes='box',wrap=True,markup=False,auto_scroll=True)
        yield Static('Connecting to receiver…',id='health',markup=False)
        yield Footer()

    def on_mount(self):
        columns={
            'agent-table':[('Agent',18),('Project',18),('Activity',12),('Current / last task',56),('Updated',10)],
            'agent-actions':[('Time',8),('Type',9),('Last observed actions',48)],
            'activity-table':[('Time',8),('Type',7),('State',10),('Action',40)],
            'mcp-table':[('Time',8),('Agent / session',14),('MCP server',12),('Operation',17),('Result',8),('Latency',9)],
            'trace-table':[('State',11),('Task / run',31),('Spans',5),('Tokens in / out',16)],
            'host-table':[('Agent',16),('PID',9),('Project',20),('State',23),('RAM',12),('CPU total',13)]}
        for name,cols in columns.items():
            table=self.query_one('#'+name,DataTable)
            for index,(label,width) in enumerate(cols):table.add_column(label,width=width,key=str(index))
        for name,title in [('agent-table','Agent sessions'),('agent-detail','Selected agent'),('agent-actions','Recent work'),('activity-table','Observed activity'),('mcp-table','MCP operations in current scope'),('trace-table','Traces'),('trace-detail','Spans in selected trace'),('host-table','Known agent processes'),('host-detail','Host & collection status')]:
            self.query_one('#'+name).border_title=title
        self.query_one('#host-detail').styles.height=8
        if not self.feed.thread.is_alive():self.feed.thread.start()
        self.set_interval(.3,self.refresh_data)
        self.query_one('#agent-table',DataTable).focus()

    def on_resize(self):
        self.screen.set_class(self.size.width<110,'narrow')

    def stat(self,name,value,label,detail):
        text=Text(str(value),style='bold #a9d1ff');text.append('  '+label,style='bold');text.append('\n'+detail,style='#91a7c3')
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

    def refresh_data(self):
        data=self.feed.snapshot
        connection='PAUSED' if self.feed.paused else 'RECONNECTING' if data.error else 'CONNECTED'
        self.sub_title=connection+' · '+self.feed.api.endpoint
        self.query_one('#health',Static).update(Text(data.error or (
            f"Updated {age(int(data.updated*1e9))} · Scope: {self.scope_label()} · Local session records + received telemetry · Reviews {'shown' if self.feed.include_reviews else 'hidden'}"),style='red' if data.error else '#91a7c3'))
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
        self.stat('sessions',len(work),'agent '+('session' if len(work)==1 else 'sessions'),f'{recent} recent · {len(work)-recent} idle / offline · {reviews} reviews')
        self.stat('processes',live,'live processes','Observed on this host; separate from sessions')
        servers={item.get('server') for item in data.mcp if item.get('server') and item.get('server')!='Not reported'}
        operations={(item.get('server'),item.get('tool')) for item in data.mcp if item.get('tool') and item.get('tool')!='Not reported'}
        mcp_errors=sum(str(item.get('status','')).upper() in {'ERROR','FAILED'} for item in data.mcp)
        self.stat('mcp-count',quantity(len(data.mcp),'MCP operation'),'observed',f'{quantity(len(servers),"server")} · {quantity(len(operations),"tool")} · {quantity(mcp_errors,"error")} in scope')
        self.stat('usage',number(data.totals.get('input_tokens'))+' / '+number(data.totals.get('output_tokens')),'input / output tokens','Stored usage; input may include cached context')
        options=[('All agent sessions','all')]+[(f"{item['runtime']} · {item.get('project') or 'SDK'} · {(item.get('session_id') or item['id'])[-6:]}",item['id']) for item in agents]
        if options!=self.scope_options:
            picker=self.query_one('#scope',Select);old=self.scope_value
            picker.set_options(options);picker.value=old if any(value==old for _,value in options) else 'all';self.scope_options=options
        self.source_map={item['id']:item for item in agents}
        self.fill('agent-table',[(item['id'],[cell(item['runtime']+' · '+(item.get('session_id') or item['id'])[-6:]),cell(item.get('project') or '—'),cell(item['state'],'green' if item['recent'] else 'dim'),cell(item.get('task') or 'Task not reported'),cell(age(item['stamp']))],item) for item in agents])
        if self.selected_source not in self.source_map:self.selected_source=agents[0]['id'] if agents else None
        self.show_agent()
        activity=[(item['source_id'],[cell(time.strftime('%H:%M:%S',time.localtime(item['time_unix_nano']/1e9))),cell(item['domain'].upper(),'#92b9eb'),status(item.get('result')),cell(headline(item))],item) for item in data.activity]
        self.fill('activity-table',activity)
        if self.selected_activity not in self.table_data.get('activity-table',{}):self.selected_activity=activity[0][0] if activity else None
        self.show_activity()
        self.fill('mcp-table',[(item['id'],[cell(table_when(item['time_unix_nano'])),cell((item.get('service') or 'Unknown')+' · '+str(item.get('session_id') or '—')[-6:]),cell(item['server']),cell(item['tool']),status(item['status']),cell(timing(item['duration_ms']))],item) for item in data.mcp])
        self.query_one('#mcp-table').border_subtitle=f"{quantity(len(data.mcp),'operation')} in current scope"
        if self.selected_mcp not in self.table_data.get('mcp-table',{}):self.selected_mcp=data.mcp[0]['id'] if data.mcp else None
        self.show_mcp()
        if not data.mcp_available:
            mcp_hint='This receiver version does not expose MCP operations. Update and restart the receiver.'
        elif data.mcp:
            active=sum(str(item.get('status','')).upper()=='RUNNING' for item in data.mcp)
            mcp_hint=f"{quantity(len(data.mcp),'operation')} · {quantity(len(servers),'server')} · {quantity(len(operations),'tool')} · {quantity(mcp_errors,'error')} · {quantity(active,'in-progress call')} · {self.scope_label()}"
        elif self.feed.query:
            mcp_hint=f"No MCP operations match “{self.feed.query}” · {self.scope_label()}"
        else:
            mcp_hint=f"No MCP operations in {self.scope_label()} · Check MCP instrumentation; general tool activity remains available in Activity."
        self.query_one('#mcp-hint',Static).update(mcp_hint)
        traces=data.traces[:]
        detail=data.detail
        if self.feed.pinned_trace and detail.get('summary') and not any(item['trace_id']==detail['trace_id'] for item in traces):traces.insert(0,detail['summary'])
        self.fill('trace-table',[(item['trace_id'],[cell('Active · errors','yellow') if item.get('active') and item['status']=='ERROR' else cell('Error','red') if item['status']=='ERROR' else status(item['status']),cell(trace_name(item)),cell(item['span_count']),cell(number(item['input_tokens'])+' / '+number(item['output_tokens']))],item) for item in traces],preserve=self.feed.selected_trace)
        self.show_spans()
        self.fill('host-table',[(str(item['pid']),[cell(item['name']),cell(item['pid']),cell(item.get('project') or '—'),cell(item['state']),cell(f"{item['memory_rss_kib']/1024:.0f} MiB"),cell(f"{item['cpu_seconds']:.1f} s")],item) for item in processes])
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
        self.inspector_signatures[name]=signature
        log=self.query_one('#'+name,RichLog);log.clear();log.write(Text(title,style='bold #a9d1ff'))
        table=Table.grid(padding=(0,2));table.add_column(style='#91a7c3',no_wrap=True);table.add_column()
        for label,value in fields:table.add_row(Text(label),Text(str(value or 'Not reported')))
        log.write(table)
        for label,value in sections:self.write_evidence(log,label,value)

    def write_evidence(self,log,label,value,depth=0):
        if value is None or value=='':return
        value=decode(value)
        if isinstance(value,(dict,list)) and not value:
            log.write(Text('\n'+label.title(),style='bold #92b9eb'))
            log.write(Text('No entries' if isinstance(value,list) else 'Empty object'))
            return
        if depth<7 and isinstance(value,dict):
            if value.get('type') in {'text','input_text','output_text'} and 'text' in value:
                self.write_evidence(log,label,value['text'],depth+1);return
            for key,child in value.items():
                self.write_evidence(log,str(key).replace('_',' '),child,depth+1)
            return
        if depth<7 and isinstance(value,list):
            for child in value:self.write_evidence(log,label,child,depth+1)
            return
        log.write(Text('\n'+label.title(),style='bold #92b9eb'))
        if isinstance(value,(dict,list)):value=json.dumps(value,ensure_ascii=False,indent=2)
        if label.lower() in {'command','diff','patch','unified diff','code'}:
            lexer='diff' if 'diff' in label.lower() or label.lower()=='patch' else 'python' if label.lower()=='code' else 'bash'
            log.write(Syntax(str(value),lexer,word_wrap=True,background_color='#101e31'))
        else:log.write(Text(str(value)))

    def show_agent(self):
        item=getattr(self,'source_map',{}).get(self.selected_source)
        if not item:
            self.inspect('agent-detail','No agent sessions found',[],[('Next step','Local Codex and Claude Code sessions are discovered automatically. Python SDK agents can use the capture launcher.')]);return
        summary=next((entry for entry in self.data.sessions if item.get('session_id') and entry.get('session_id')==item.get('session_id')),{})
        latest=item.get('last_activity') or {}
        self.inspect('agent-detail',f"{item['runtime']} · {item.get('model') or item.get('project') or 'SDK'}",[
            ('Task',clean(item.get('task') or 'Task not reported')[:130]),
            ('Last action',headline(latest)[:100] if latest else 'Not reported'),
            ('Model',item.get('model') or 'Not reported'),('Observed',f"{item['state']} · {age(item.get('stamp'))}"),
            ('Work',f"{summary.get('trace_count',0)} traces · {item.get('spans',0)} spans · {number(summary.get('input_tokens'))} in / {number(summary.get('output_tokens'))} out"),
            ('Cost','Not reported' if not summary or summary.get('unknown_cost_runs') else f"${summary.get('known_cost_usd',0):.4f} known")],
            [('Full task',item.get('task')),('Session',item.get('session_id') or item['id'])])
        actions=[event for event in self.data.activity if event.get('session_id')==item.get('session_id')] if item.get('session_id') else [event for event in self.data.activity if event.get('service')==item.get('service')]
        meaningful=[event for event in actions if event.get('domain') in {'shell','file','mcp','browser','model','message'}]
        actions=meaningful or actions
        self.fill('agent-actions',[(event['source_id'],[cell(time.strftime('%H:%M:%S',time.localtime(event['time_unix_nano']/1e9))),cell(event['domain'].upper()),cell(headline(event))],event) for event in actions[:30]])

    def show_activity(self):
        item=self.table_data.get('activity-table',{}).get(self.selected_activity)
        if not item:
            self.inspect('activity-detail','No activity in this scope',[],[('Scope','Choose another agent session or clear the search to view recent activity.')]);return
        details=decode(item.get('detail') or '')
        sections=list(details.items()) if isinstance(details,dict) else [('Recorded content',details)]
        self.inspect('activity-detail',headline(item),[('Agent',item.get('service')),('Type',item.get('domain')),('Result',str(status(item.get('result')))),
            ('When',time.strftime('%H:%M:%S',time.localtime(item['time_unix_nano']/1e9))),('Duration',timing(item.get('duration_ms'))),
            ('Trace',item.get('trace_id')),('Span',item.get('span_id') or 'Not linked'),('Source',item.get('source'))],sections)

    def show_mcp(self):
        item=self.table_data.get('mcp-table',{}).get(self.selected_mcp)
        if not item:
            if not self.data.mcp_available:
                title='MCP inventory unavailable'
                note='The connected receiver is too old to provide this endpoint. Update and restart the receiver.'
            elif self.feed.query:
                title='No matching MCP operations'
                note='Clear Search or choose another agent session. Search also matches server, tool, arguments, and result.'
            else:
                title='No MCP operations in this scope'
                note='Calls identified as Model Context Protocol appear here. Other tool activity remains in Activity.'
            self.inspect('mcp-detail',title,[('Scope',self.scope_label())],[('How to proceed',note)])
            return
        self.inspect('mcp-detail',item['server']+' / '+item['tool'],[
            ('Agent',item.get('service') or 'Not reported'),('Session',item.get('session_id') or 'Not reported'),
            ('Method',item.get('method') or 'Not reported'),('Result',str(status(item.get('status')))),
            ('Latency',timing(item.get('duration_ms'))),('Observed',when(item.get('time_unix_nano') or 0)),
            ('Trace ID',item.get('trace_id') or 'Not linked'),('Span ID',item.get('span_id') or 'Not linked'),
            ('Evidence',item.get('source') or 'Not reported')],[('Arguments',item.get('arguments')),('Result',item.get('output'))])

    def show_spans(self):
        detail=self.data.detail
        if detail.get('trace_id')!=self.feed.selected_trace:return
        spans=detail.get('spans',[])
        signature=(detail.get('trace_id'),tuple((item['span_id'],item['status'],item['end_unix_nano'],str(item['attributes'])) for item in spans))
        if signature==self.span_signature:return
        self.span_signature=signature
        tree=self.query_one('#span-tree',Tree);tree.clear()
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
            self.call_after_refresh(tree.select_node,nodes[self.jump_span]);self.jump_span=None

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
        if event.data_table.id=='agent-table':
            self.selected_source=item['id'];self.show_agent();self.query_one('#agent-detail',RichLog).focus()
        elif event.data_table.id=='agent-actions':
            self.selected_activity=item['source_id'];self.action_page('activity');self.show_activity()
        elif event.data_table.id=='activity-table':self.query_one('#activity-detail',RichLog).focus()
        elif event.data_table.id=='mcp-table':self.query_one('#mcp-detail',RichLog).focus()

    @on(Tree.NodeHighlighted,'#span-tree')
    def span_highlighted(self,event):
        span=event.node.data
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

    @on(Input.Changed,'#search')
    def search_changed(self,event):
        if self.search_timer:self.search_timer.stop()
        value=event.value
        def apply():
            self.feed.query=value.strip();self.feed.selected_trace=None;self.feed.pinned_trace=False;self.feed.request_refresh()
            self.feed.selected_span_id=None
        self.search_timer=self.set_timer(.3,apply)

    @on(Checkbox.Changed,'#reviews')
    def reviews_changed(self,event):
        self.feed.include_reviews=event.value;self.feed.selected_trace=None;self.feed.pinned_trace=False;self.feed.request_refresh()
        self.feed.selected_span_id=None

    @on(Button.Pressed)
    def button_pressed(self,event):
        if event.button.id=='pause':self.action_pause()
        elif event.button.id=='agent-open':self.action_focus_agent()
        elif event.button.id in {'activity-trace','mcp-trace'}:self.action_open_trace()

    def action_pause(self):
        self.feed.paused=not self.feed.paused;self.query_one('#pause',Button).label='Resume' if self.feed.paused else 'Pause';self.feed.request_refresh();self.refresh_data()

    def action_search(self):self.query_one('#search',Input).focus()
    def action_back(self):
        self.action_page(self.query_one('#pages',TabbedContent).active)
    def action_focus_agent(self):
        if self.selected_source:
            self.query_one('#scope',Select).value=self.selected_source
            self.action_page('activity')
    def action_reviews(self):self.query_one('#reviews',Checkbox).toggle()
    def action_all(self):
        self.query_one('#scope',Select).value='all';self.query_one('#search',Input).value=''
    def action_page(self,page):
        if page=='output' and not self.agent:return
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
