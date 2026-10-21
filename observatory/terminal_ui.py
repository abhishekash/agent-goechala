"""Agent-first terminal UI. Raw evidence lives in scrollable inspectors."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from rich.cells import cell_len, get_character_cell_size
from rich.segment import Segment
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import Screen
from textual.strip import Strip
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, RichLog, Select, Static, TabbedContent, TabPane, Tree

from .terminal import clean, number, trace_name
from .branding import NAME, AMBER, LAVENDER, MINT, ICE, masthead, tower_mark
from .themes import PALETTES, supported_themes, themed_style, themed_text


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


def reported_number(value):
    return 'Not reported' if value is None else number(value)


def observed_count(record, key):
    if record.get(key + '_reported') is False:
        return 'Not reported'
    return reported_number(record.get(key))


def when(stamp):
    value=time.localtime(stamp/1e9)
    return time.strftime('%H:%M:%S' if value[:3]==time.localtime()[:3] else '%m-%d %H:%M',value)


def table_when(stamp):
    value=time.localtime(stamp/1e9)
    return time.strftime('%H:%M:%S' if value[:3]==time.localtime()[:3] else '%m-%d',value)


def metric_value(point):
    data=(point or {}).get('data') or {}
    try:
        if int(data.get('flags',0)) & 1:
            return 'No recorded value'
    except (TypeError,ValueError,OverflowError):
        pass
    if 'value' in data:
        return str(data['value'])
    if 'count' in data:
        return 'count '+str(data['count'])+(' · sum '+str(data['sum']) if 'sum' in data else '')
    if data.get('quantileValues'):
        return ', '.join(f"{item.get('quantile', '?')}: {item.get('value', '?')}"
                         for item in data['quantileValues'][:3])
    return 'distribution'


def usage_series_text(points, service=None, session_id=None):
    """Render token and run history without implying missing usage is zero."""
    points = sorted(points or [], key=lambda item: item.get('start_unix_nano') or 0)[-7:]
    scope = service or 'all services'
    if session_id:
        scope += ' · session ' + session_id[-8:]
    if not points:
        return f"TOKEN USAGE · LAST 7 DAYS · {scope}\nNo usage history recorded in this scope."

    def total(key):
        if not any(point.get(key + '_reported') for point in points):
            return 'Not reported'
        return number(sum(int(point.get(key) or 0) for point in points))

    lines = [
        f"TOKEN USAGE · LAST 7 DAYS · {scope}",
        f"Input {total('input_tokens')}  ·  Output {total('output_tokens')}  ·  "
        f"Runs {sum(int(point.get('runs') or 0) for point in points)}  ·  "
        f"Errors {sum(int(point.get('errors') or 0) + int(point.get('log_errors') or 0) for point in points)}",
        f"Cache read {total('cache_read_input_tokens')} · write {total('cache_write_input_tokens')} · "
        f"reasoning {total('reasoning_output_tokens')}",
        'Cached input is included in input tokens. Daily values use local dates.',
        'DATE       INPUT       OUTPUT      RUNS  ERRORS  LOGS',
    ]
    for point in points:
        stamp = point.get('start_unix_nano')
        day = time.strftime('%b %d', time.localtime(stamp / 1e9)) if stamp else 'Unknown'
        input_value = number(point.get('input_tokens') or 0) if point.get('input_tokens_reported') else '—'
        output_value = number(point.get('output_tokens') or 0) if point.get('output_tokens_reported') else '—'
        lines.append(f"{day:<10} {input_value:>10}  {output_value:>10}  "
                     f"{int(point.get('runs') or 0):>4}  "
                     f"{int(point.get('errors') or 0) + int(point.get('log_errors') or 0):>6}  "
                     f"{int(point.get('log_events') or 0):>4}")
    return '\n'.join(lines)


def status(value):
    label={'OK':'OK','UNSET':'Observed','RUNNING':'In progress','ERROR':'Error','FAILED':'Failed','INCOMPLETE':'Incomplete',
           'success':'OK','started':'Started','completed':'Completed','observed':'Observed','reported':'Reported','error':'Error'}.get(str(value),str(value).title())
    color='#ff8799' if str(value).lower() in {'error','failed','denied','blocked','incomplete'} else '#86e3b5' if str(value).lower() in {'success','ok','completed'} else '#ffbd69' if str(value).lower() in {'running','started'} else '#b9afc5'
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


def embedded_json(text):
    """Split prose from complete JSON objects embedded in a text record."""
    decoder=json.JSONDecoder()
    segments=[]
    cursor=0
    index=0
    while index<len(text):
        if text[index] not in '{[':
            index+=1
            continue
        try:
            value,end=decoder.raw_decode(text,index)
        except (ValueError,TypeError):
            index+=1
            continue
        if not isinstance(value,(dict,list)) or end<=cursor:
            index+=1
            continue
        if text[cursor:index]:segments.append(('text',text[cursor:index]))
        segments.append(('json',value))
        cursor=end
        index=end
    if not segments:return [('text',text)]
    if text[cursor:]:segments.append(('text',text[cursor:]))
    return segments


def format_json(value):
    """Return indented JSON and multiline string fields for readable display."""
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

    return json.dumps(prepare(value),ensure_ascii=False,indent=2),text_values


def write_json(log,value,theme='Current'):
    """Render indented JSON and move multiline string values into readable blocks."""
    formatted,text_values=format_json(value)
    log.write(evidence_syntax(formatted,'json',theme))
    for path,content in text_values:
        log.write(Text(f'\n{path} · {len(content.splitlines())} lines',style='bold #c5adff'))
        log.write(Text(content))


def copy_event_text(item):
    """Build a readable plain-text export of one selected activity or MCP event."""
    is_mcp=bool(item.get('server') or item.get('tool') or item.get('domain')=='mcp')
    title=f"{item.get('server') or 'MCP'} / {item.get('tool') or 'tool'}" if is_mcp else headline(item)
    lines=[title,f"Agent: {item.get('service') or 'Not reported'}",
           f"Type: {item.get('domain') or ('mcp' if is_mcp else 'Not reported')}",
           f"Result: {item.get('result') or item.get('status') or 'Not reported'}",
           f"When: {when(item['time_unix_nano']) if item.get('time_unix_nano') else 'Not reported'}",
           f"Duration: {timing(item.get('duration_ms'))}"]
    if item.get('trace_id'):lines.append(f"Trace ID: {item['trace_id']}")
    if item.get('span_id'):lines.append(f"Span ID: {item['span_id']}")
    lines.append('\nRecorded content')
    def append_json(value):
        formatted,text_values=format_json(value)
        lines.append(formatted)
        for path,content in text_values:lines.extend((f'\n{path}',content))
    if is_mcp:
        details={key:item.get(key) for key in ('server','tool','method','arguments','output') if item.get(key) is not None}
    else:
        details=decode_nested(item.get('detail') or '')
    if isinstance(details,(dict,list)):
        append_json(details)
    elif isinstance(details,str):
        for kind,content in embedded_json(details):
            if kind=='json':append_json(content)
            elif content.strip():lines.append(content.strip('\n'))
    elif details:
        lines.append(str(details))
    return '\n'.join(lines)


async def write_system_clipboard(text):
    """Write to the desktop clipboard when a native clipboard tool is available."""
    candidates=[]
    if sys.platform=='darwin' and shutil.which('pbcopy'):
        candidates.append((['pbcopy'],'macOS'))
    elif os.name=='nt' and shutil.which('clip.exe'):
        candidates.append((['clip.exe'],'Windows'))
    elif sys.platform.startswith('linux'):
        if os.environ.get('WAYLAND_DISPLAY') and shutil.which('wl-copy'):
            candidates.append((['wl-copy'],'Wayland'))
        if os.environ.get('DISPLAY') and shutil.which('xclip'):
            candidates.append((['xclip','-selection','clipboard','-i'],'X11'))
        if os.environ.get('DISPLAY') and shutil.which('xsel'):
            candidates.append((['xsel','--clipboard','--input'],'X11'))
    for command,label in candidates:
        try:
            # Clipboard owners such as xclip fork a background process. Captured
            # pipes can stay open in that child even after the command succeeds.
            process=await asyncio.create_subprocess_exec(*command,stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        except OSError:
            continue
        try:
            await asyncio.wait_for(process.communicate(text.encode('utf-8')),timeout=3)
        except (asyncio.TimeoutError,asyncio.CancelledError) as exc:
            if process.returncode is None:
                try:process.kill()
                except ProcessLookupError:pass
            await process.wait()
            if isinstance(exc,asyncio.CancelledError):raise
            continue
        if process.returncode==0:
            return label
    return None


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
        reported=entry.get('run_state','unknown')
        state={'completed':'Completed','interrupted':'Interrupted','waiting':'Waiting','running':'Working'}.get(reported,'Active' if recent else 'Unknown')
        if reported in {'running','waiting'} and not recent:
            state='No updates'
        result.append({**entry,'id':entry['source_id'],'review':review,'recent':recent,'stamp':stamp,
                       'state':state,'spans':connection.get('span_count',0)})
    for entry in connections.values():
        if 'local-session-log' in entry.get('integrations',[]):
            continue
        result.append({**entry,'id':entry['source_id'],'session_id':None,'project':'SDK / OTLP','model':'',
                       'runtime':entry.get('service'),'task':'Task not reported','recent':entry['state']=='running',
                       'state':'Connected' if entry['state']=='running' else 'Finished' if entry['state']=='stopped' else 'Offline',
                       'stamp':entry.get('last_seen_ns',0),'spans':entry.get('span_count',0),'review':False})
    known_sessions={item.get('session_id') for item in result if item.get('session_id')}
    for session in getattr(snapshot,'sessions',[]):
        session_id=session.get('session_id')
        if not session_id or session_id in known_sessions:
            continue
        known_sessions.add(session_id)
        services=session.get('services') or []
        service=services[0] if len(services)==1 else ''
        stamp=session.get('last_unix_nano') or 0
        recent=bool(stamp and time.time()-stamp/1e9<35)
        reported=session.get('latest_status','UNKNOWN')
        waiting_for=session.get('waiting_for') or ''
        if reported in {'RUNNING','WAITING'}:
            state=('No updates' if not recent else
                   'Waiting' if reported=='WAITING' or waiting_for else 'Working')
        else:
            state={'ERROR':'Error','FAILED':'Failed','INCOMPLETE':'Incomplete',
                   'OK':'Completed'}.get(reported,'Unknown')
        latest_activity=max((event for event in snapshot.activity
                             if event.get('session_id')==session_id),
                            key=lambda event:event.get('time_unix_nano',0),default=None)
        runtime='Gemini CLI' if any('gemini' in name.casefold() for name in services) else 'OpenTelemetry'
        result.append({'id':'otlp-session:'+str(session_id),'source_id':'otlp-session:'+str(session_id),
            'session_id':session_id,'runtime':runtime,'service':service,'project':session.get('project') or '',
            'working_directory':'','branch':'','model':', '.join(session.get('models') or []),
            'task':session.get('latest_task') or '', 'run_state':reported.casefold(),
            'waiting_for':waiting_for,'last_activity':latest_activity,'capture_content':False,
            'recent':recent,'stamp':stamp,'state':state,'spans':session.get('span_count',0),
            'trace_count':session.get('trace_count',0),'review':False})
    return sorted(result,key=lambda item:(not item['recent'],-item['stamp'],item['id']))


def wait_summary(item):
    """Describe a recorded wait without implying this read-only monitor can answer it."""
    reason=str(item.get('waiting_for') or '').strip()
    folded=reason.casefold()
    runtime=item.get('runtime') or 'agent'
    if 'approval' in folded or 'permission' in folded:
        return f'Approval needed · review it in the {runtime} terminal; Goechala is read-only.'
    if 'user input' in folded or 'input request' in folded:
        return f'Your reply is needed · answer in the {runtime} terminal; Goechala is read-only.'
    if reason.lower().startswith('tool response:'):
        return f'{reason} · tool response is pending; no user action was reported.'
    if item.get('state')=='Waiting':
        return 'Waiting · reason not reported; check the agent terminal.'
    if item.get('state') in {'Completed'}:
        return 'The agent reported completion.'
    if item.get('state') in {'Error','Failed','Incomplete'}:
        return 'Review the recorded error in Activity or Traces.'
    return 'Remaining work is not reported by the agent.'


def recorded_progress(item, events, totals):
    """Render a fast, zero-setup progress summary from observed records only."""
    recent=sorted(events,key=lambda event:event.get('time_unix_nano',0),reverse=True)
    latest=recent[0] if recent else item.get('last_activity') or {}
    errors=totals.get('errors') if isinstance(totals,dict) else None
    if isinstance(errors,bool) or not isinstance(errors,(int,float)):
        errors=None
    if errors is None:
        errors=sum(str(event.get('result') or '').casefold() in
                   {'error','failed','denied','blocked','policy_denied','incomplete'}
                   for event in recent)
        error_note=f'{errors} recorded in captured recent activity'
    else:
        error_note=f'{int(errors)} recorded across observed runs'
    latest_note=clean(headline(latest))[:120] if latest else 'No activity captured yet.'
    return (f"Current: {item.get('state') or 'Unknown'}\n"
            f"Latest: {latest_note}\n"
            f"Errors: {error_note}\n"
            f"Next: {wait_summary(item)}")


def wait_label(item):
    """Use compact but actionable wording in the session list."""
    reason=str(item.get('waiting_for') or '').strip()
    folded=reason.casefold()
    runtime=item.get('runtime') or 'agent'
    if 'approval' in folded or 'permission' in folded:
        return f'Approval · {runtime} terminal'
    if 'user input' in folded or 'input request' in folded:
        return f'Reply · {runtime} terminal'
    return reason or '—'


def task_text(item):
    """Keep real tasks prominent and missing descriptions quiet."""
    task=clean(' '.join(str(item.get('task') or '').split()))
    if not task or task=='Task not reported':
        return Text('Not reported',style='italic #b9afc5',no_wrap=True)
    return Text(task,style='#f6f0e8',no_wrap=False,overflow='ellipsis')


def input_status(item):
    """Name only user actions explicitly reported by a session."""
    if not input_required(item):
        return item.get('state') or 'Unknown'
    reason=str(item.get('waiting_for') or '').casefold()
    return 'NEEDS APPROVAL' if 'approval' in reason or 'permission' in reason else 'NEEDS REPLY'


def input_required(item):
    reason=str(item.get('waiting_for') or '').casefold()
    return item.get('state')=='Waiting' and any(
        marker in reason for marker in ('approval','permission','user input','input request'))


def coverage_warning(local_status):
    """Report discovery caps so a partial session list is not mistaken for all agents."""
    omitted=[]
    if local_status.get('omitted_sources'):
        omitted.append(f"{local_status['omitted_sources']} older Codex/Claude sessions beyond the {local_status.get('max_sources',50)}-session limit")
    if local_status.get('opencode_omitted_sessions'):
        omitted.append(f"{local_status['opencode_omitted_sessions']} older OpenCode sessions beyond the 50-session limit")
    return 'Coverage limited: '+'; '.join(omitted) if omitted else ''


class ResultsTable(DataTable):
    """Size columns to the panel while keeping the selected record on resize."""
    def preview_height(self,values):
        if self.id!='agent-table' or not self.column_widths or not values:
            return 1
        task=values[-1]
        return min(2,max(1,len(task.wrap(self.app.console,self.column_widths[-1]))))

    def add_row(self,*cells,**kwargs):
        kwargs.setdefault('height',self.preview_height(cells))
        return super().add_row(*cells,**kwargs)

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


class SearchInput(Input):
    """Use familiar select-all behavior in cockpit search fields."""
    BINDINGS=[Binding('ctrl+a','select_all','Select all',show=False)]


def evidence_syntax(content, lexer, theme='Current'):
    return Syntax(content, lexer, word_wrap=True,
                  theme='friendly' if theme == 'Bright' else 'monokai',
                  background_color=PALETTES[theme]['surface'])


class SelectableRichLog(RichLog):
    """Selectable evidence with visible highlighting and plain-text extraction."""
    def write(self, content, *args, **kwargs):
        if isinstance(content, Text):
            content = themed_text(self, content)
        return super().write(content, *args, **kwargs)

    def render_line(self,y):
        source_y=self.scroll_offset.y+y
        width=self.scrollable_content_region.width
        if source_y>=len(self.lines):
            # Map empty space to the end, so dragging below the text is safe.
            last=len(self.lines)-1
            return Strip.blank(width,self.rich_style).apply_offsets(
                len(self.lines[last].text) if last>=0 else 0,max(0,last))
        line=self.lines[source_y].apply_style(self.rich_style)
        selection=self.text_selection
        span=selection.get_span(source_y) if selection else None
        if span:
            start,end=span
            text=line.text
            start=cell_len(text[:start])
            end=line.cell_length if end==-1 else cell_len(text[:end])
            if end>start:
                selected=line.crop(start,end)
                highlight=self.screen.get_component_rich_style('screen--selection')
                selected=Strip(Segment.apply_style(selected,post_style=highlight),selected.cell_length)
                line=Strip.join([line.crop(0,start),selected,line.crop(end)])
        cells=characters=0
        for character in line.text:
            if cells>=self.scroll_offset.x:break
            cells+=get_character_cell_size(character)
            characters+=1
        return line.crop_extend(self.scroll_offset.x,self.scroll_offset.x+width,self.rich_style).apply_offsets(characters,source_y)

    @property
    def plain_text(self):
        return '\n'.join(line.text.rstrip() for line in self.lines)

    def get_selection(self,selection):
        if not self.lines:return None
        selected=[]
        for y,line in enumerate(self.lines):
            span=selection.get_span(y)
            if span is not None:
                start,end=span
                selected.append(line.text[start:None if end==-1 else end].rstrip())
        return '\n'.join(selected),'\n'

    def clear(self):
        # A selection from a previous event must never copy the next event.
        if self.is_mounted and self in self.screen.selections:
            self.screen.clear_selection()
        return super().clear()


class ObservatoryScreen(Screen):
    """Copy pointer-selected text through the desktop clipboard when possible."""
    def action_copy_text(self):
        selected=self.get_selected_text()
        if selected is None:
            self.app.notify('Drag across text to select it, then press Ctrl+C.')
            return
        self.app.copy_to_clipboard(selected)


class ObservatoryApp(App):
    TITLE=NAME
    AUTO_FOCUS='#agent-table'
    CSS='''
    Screen { background: $agent-canvas; color: $agent-text; }
    Screen > .screen--selection { background: $agent-selection; color: $agent-on-selection; }
    * { scrollbar-color: $agent-scrollbar; scrollbar-color-hover: $agent-secondary; scrollbar-color-active: $agent-primary; scrollbar-background: $agent-surface; scrollbar-background-hover: $agent-surface; scrollbar-background-active: $agent-surface; }
    #masthead { layout: grid; grid-size: 3; grid-columns: 1fr auto 1fr; grid-rows: 5; height: 6; padding: 0 2; align-vertical: middle; background: $agent-canvas; border-bottom: solid $agent-border-subtle; }
    #brand-lockup { width: auto; height: 5; align-vertical: middle; }
    #tower { width: 5; height: 5; margin-right: 2; }
    #brand { width: auto; height: 1; color: $agent-text; text-wrap: nowrap; }
    #connection { width: 100%; height: 1; color: $agent-success; text-align: right; text-wrap: nowrap; text-overflow: ellipsis; }
    #summary { height: 3; margin: 1 2; grid-size: 4; grid-columns: 1fr; grid-rows: 3; }
    .stat { height: 3; min-width: 0; border-left: solid $agent-border-subtle; background: $agent-surface; padding: 0 2; text-wrap: nowrap; text-overflow: ellipsis; }
    #stat-sessions { border-left: solid $agent-primary; }
    #stat-processes { border-left: solid $agent-success; }
    #stat-mcp-count { border-left: solid $agent-secondary; }
    #stat-usage { border-left: solid $agent-info; }
    #controls { height: 3; margin: 0 2; align-vertical: middle; }
    #scope-label { width: 6; height: 1; margin-top: 1; color: $agent-muted; }
    #scope { width: 32; margin-right: 1; }
    SelectCurrent { background: $agent-panel; border: tall $agent-border; }
    SelectCurrent #label { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    Select:focus > SelectCurrent { border: tall $agent-primary; background-tint: transparent; }
    SelectOverlay { background: $agent-panel; border: tall $agent-primary; }
    #controls-spacer { width: 1fr; }
    #reviews { width: 14; border: none; background: $agent-canvas; margin-top: 1; color: $agent-muted; }
    Checkbox > .toggle--button { color: $agent-disabled; background: $agent-panel; }
    Checkbox.-on > .toggle--button { color: $agent-primary; }
    Button { height: 3; min-width: 9; padding: 1 0 0 0; background: $agent-panel; color: $agent-label; border: none; border-bottom: solid $agent-border; text-style: none; }
    Button:hover { background: $agent-hover; border-bottom: solid $agent-secondary; }
    Button:focus { border-bottom: heavy $agent-primary; background: $agent-focus; color: $agent-primary; text-style: bold; }
    Button:disabled { color: $agent-disabled; border-bottom: solid $agent-border-disabled; background: $agent-surface; }
    #pause { width: 12; }
    #pages { height: 1fr; margin: 0 2; }
    Tabs { height: 2; padding-top: 0; margin-top: 1; }
    Tab { color: $agent-muted; padding: 0 2; background: $agent-surface; }
    Tab.-active { color: $agent-on-primary; background: $agent-primary; text-style: bold; }
    Tabs:focus .-active { background: $agent-primary-focus; color: $agent-on-primary; text-style: bold underline; }
    Underline > .underline--bar { color: $agent-primary; background: $agent-border; }
    TabPane { padding: 0; }
    ContentSwitcher { height: 1fr; }
    .hint { height: 2; color: $agent-muted; }
    .box { border: round $agent-border-subtle; border-title-color: $agent-label; border-title-style: bold; background: $agent-surface; padding: 0 1; }
    .box:focus-within { border: round $agent-primary; border-title-color: $agent-primary; }
    .results { height: 1fr; }
    .searchbar { height: 3; align-vertical: middle; }
    .view-search { width: 1fr; margin-right: 1; padding: 0 1; background: $agent-panel; border: tall $agent-border; }
    .view-search:focus { border: tall $agent-primary; background-tint: transparent; }
    Input > .input--placeholder { color: $agent-muted; }
    Input > .input--cursor { background: $agent-primary; color: $agent-canvas; }
    Input > .input--selection { background: $agent-selection; color: $agent-on-selection; }
    .clear-search { width: 9; }
    .error-filter { width: 12; margin-right: 1; }
    .error-filter.-on { color: $agent-error; border-bottom: heavy $agent-error; background: $agent-error-surface; text-style: bold; }
    .prompt-filter { width: 12; margin-right: 1; }
    .prompt-filter.-on { color: $agent-primary; border-bottom: heavy $agent-primary; background: $agent-focus; text-style: bold; }
    .search-status { height: 1; color: $agent-muted; padding: 0 1; }
    .history-controls { height: 1; margin-top: 1; }
    .history-controls Static { width: 1fr; color: $agent-muted; }
    .history-controls Button { min-width: 8; width: auto; height: 1; padding: 0 1; margin-left: 1; border: none; }
    .history-controls Button:focus { background: $agent-cursor; color: $agent-on-selection; }
    #history-back { width: 10; margin-right: 1; }
    #history-back:disabled { display: none; }
    DataTable { background: $agent-surface; height: 1fr; scrollbar-size: 1 1; }
    DataTable:focus { background-tint: transparent; }
    DataTable > .datatable--header { background: $agent-header; color: $agent-secondary; text-style: bold; }
    DataTable > .datatable--cursor { background: $agent-cursor; color: $agent-on-cursor; text-style: bold; }
    DataTable:blur > .datatable--cursor { background: $agent-header; color: $agent-text; text-style: none; }
    DataTable > .datatable--hover { background: $agent-row-hover; }
    DataTable > .datatable--odd-row { background: $agent-row-alt; }
    DataTable > .datatable--even-row { background: $agent-surface; }
    RichLog { background: $agent-surface; color: $agent-text; scrollbar-size: 1 1; }
    RichLog:focus { background-tint: transparent; }
    #mcp-servers { height: 7; margin-bottom: 1; }
    .expanded #mcp-servers { display: none; }
    #agent-table { height: 1fr; min-height: 3; }
    #agent-bottom { height: 14; margin-top: 1; }
    #agent-info { width: 1fr; margin-right: 1; }
    #agent-detail { width: 100%; height: 1fr; }
    #agent-open { height: 3; width: 1fr; }
    #agent-actions { width: 1fr; }
    .split { height: 1fr; }
    .list { width: 3fr; margin-right: 1; }
    .detail { width: 2fr; }
    .detail-log { height: 1fr; }
    #usage-series { height: 12; min-height: 6; padding: 0 1; color: $agent-secondary; }
    .detail-button { height: 3; width: 100%; }
    .inspector-actions { height: 3; }
    .inspector-actions Button { width: 1fr; }
    .expand-button { min-width: 12; }
    #trace-detail { width: 2fr; }
    #span-tree { height: 1fr; min-height: 4; background: $agent-surface; scrollbar-size: 1 1; }
    #span-info { height: 10; border-top: solid $agent-border-subtle; }
    #health { height: 1; padding: 0 2; color: $agent-muted; }
    Footer { background: $agent-panel; color: $agent-muted; }
    FooterKey { background: $agent-footer-key; color: $agent-primary; }
    FooterKey > .footer-key--key { background: $agent-primary; color: $agent-on-primary; text-style: bold; }
    FooterKey > .footer-key--description { background: $agent-panel; color: $agent-label; }
    FooterLabel { background: $agent-panel; color: $agent-label; }
    .narrow #masthead { padding: 0 1; }
    .narrow #summary { margin: 1 1; }
    .narrow .stat { padding: 0 1; }
    .narrow #controls { margin: 0 1; }
    .narrow #scope { width: 32; }
    .narrow #pages { margin: 0 1; }
    .narrow Tab { padding: 0 1; }
    .narrow #agent-bottom { height: 12; }
    .narrow .split { layout: vertical; }
    .narrow .list { width: 100%; height: 1fr; margin-right: 0; margin-bottom: 1; }
    .narrow .detail, .narrow #trace-detail { width: 100%; height: 1fr; }
    .small #summary { height: 6; grid-size: 2; }
    .small #pause { width: 9; }
    .compact #masthead { height: 1; grid-rows: 1; border-bottom: none; }
    .compact #brand-lockup { height: 1; }
    .compact #tower { height: 1; }
    .compact #brand, .compact #connection { margin-top: 0; }
    .compact #summary { height: 2; margin: 1 1 0 1; grid-rows: 2; }
    .compact .stat { height: 2; }
    .compact.small #summary { height: 4; }
    .compact Tabs { height: 2; padding-top: 0; margin-top: 0; }
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
              Binding('c','copy','Copy'), Binding('alt+left','history_back','Back',show=False),
              Binding('[','history("newer")','Newer',show=False),Binding(']','history("older")','Older',show=False),
              Binding('l','history("latest")','Latest',show=False),
              Binding('p','pause','Pause'),Binding('a','all','Reset',show=False),Binding('v','reviews','Reviews',show=False),
              Binding('q','quit','Quit'),Binding('escape','back','Back',show=False),
              Binding('s','focus_agent','Focus agent',show=False),Binding('1','page("agents")','Agents',show=False),
              Binding('2','page("activity")','Activity',show=False),Binding('3','page("mcp")','MCP',show=False),
              Binding('4','page("traces")','Traces',show=False),Binding('5','page("host")','Host',show=False),
              Binding('6','page("metrics")','Metrics',show=False),Binding('7','page("findings")','Findings',show=False)]

    def __init__(self,feed):
        super().__init__()
        for name in list(self.available_themes):
            self.unregister_theme(name)
        for theme in supported_themes():
            self.register_theme(theme)
        self.theme = 'Current'
        self.feed=feed
        self.agent=None
        self.data=feed.snapshot
        self.last_update=0
        self.table_data={}
        self.table_signatures={}
        self.inspector_signatures={}
        self.selected_source=None
        self.selected_activity=None
        self.selected_mcp=None
        self.selected_metric=None
        self.selected_finding=None
        self.metric_history_requested_key=None
        self.metric_history_loaded_key=None
        self.metric_history_points=[]
        self.scope_options=None
        self.scope_value='all'
        self.span_signature=None
        self.search_timers={}
        self.refresh_timer=None
        self.search_queries={page:'' for page in ('agents','activity','mcp','traces','host','metrics','findings')}
        self.errors_only=set()
        self.prompts_only=False
        self.navigation_history=[]
        self.ai_summaries={}
        self.summary_busy=False
        try:
            from .ai_summary import SummaryConfig, SummaryError
            SummaryConfig.from_env()
            self.ai_summary_available=True
        except SummaryError:
            self.ai_summary_available=False

    def results(self,page,table,placeholder,classes=''):
        """Keep a view's filter, result count, and rows in one panel."""
        controls=[SearchInput(placeholder=placeholder,id='search-'+page,classes='view-search')]
        if page=='activity':
            controls.append(Button('Prompts',id='prompts-activity',classes='prompt-filter',
                                   tooltip='Show only messages you sent to the selected agent'))
        if page in {'activity','mcp'}:
            controls.append(Button('Errors',id='errors-'+page,classes='error-filter',tooltip='Show only failed calls in the current results'))
        controls.append(Button('Clear',id='clear-'+page,classes='clear-search',disabled=True))
        footer=[]
        if page in {'activity','mcp','traces','findings'}:
            footer=[Horizontal(Static('Latest',id='history-label-'+page),
                Button('Newer',id='newer-'+page,compact=True,disabled=True),
                Button('Older',id='older-'+page,compact=True,disabled=True),
                Button('Latest',id='latest-'+page,compact=True,disabled=True),classes='history-controls')]
        return Vertical(
            Horizontal(*controls,classes='searchbar'),
            Static('Loading…',id='mcp-hint' if page=='mcp' else 'search-status-'+page,classes='search-status',markup=False),
            ResultsTable(id=table,cursor_type='row',zebra_stripes=True),
            *footer,
            id='results-'+page,classes='box results '+classes)

    def compose(self)->ComposeResult:
        with Horizontal(id='masthead'):
            yield Static('', id='masthead-left')
            with Horizontal(id='brand-lockup'):
                yield Static(tower_mark(), id='tower')
                yield Static(masthead(), id='brand')
            yield Static('○ Connecting',id='connection',markup=False)
        with Grid(id='summary'):
            for name in ('sessions','processes','mcp-count','usage'):
                yield Static('',id='stat-'+name,classes='stat',markup=False)
        with Horizontal(id='controls'):
            yield Button('← Back',id='history-back',disabled=True,tooltip='Return to the event you opened · Alt+Left')
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
                        yield SelectableRichLog(id='agent-detail',classes='box',min_width=1,wrap=True,markup=False,auto_scroll=False)
                        with Horizontal(classes='inspector-actions'):
                            yield Button(Text('Session activity · s'),id='agent-open')
                            yield Button('AI summary',id='agent-summary',tooltip='Uses OPENAI_API_KEY when available. Runs only when requested; the recorded progress summary needs no setup.')
                    yield ResultsTable(id='agent-actions',classes='box',cursor_type='row',zebra_stripes=True)
            with TabPane('2  Activity',id='activity'):
                with Horizontal(classes='split'):
                    yield self.results('activity','activity-table','Filter activity by command, message, tool, or path…','list')
                    with Vertical(id='activity-inspector',classes='detail box'):
                        yield SelectableRichLog(id='activity-detail',classes='detail-log',min_width=1,wrap=True,markup=False,auto_scroll=False)
                        with Horizontal(classes='inspector-actions'):
                            yield Button(Text('Open trace · t'),id='activity-trace')
                            yield Button(Text('Copy event · c'),id='activity-copy')
                            yield Button(Text('Expand · e'),id='expand-activity',classes='expand-button')
            with TabPane('3  MCP Calls',id='mcp'):
                yield ResultsTable(id='mcp-servers',classes='box',cursor_type='row',zebra_stripes=True)
                with Horizontal(classes='split'):
                    yield self.results('mcp','mcp-table','Filter MCP operations by server, method, tool, arguments, or result…','list')
                    with Vertical(id='mcp-inspector',classes='detail box'):
                        yield SelectableRichLog(id='mcp-detail',classes='detail-log',min_width=1,wrap=True,markup=False,auto_scroll=False)
                        with Horizontal(classes='inspector-actions'):
                            yield Button(Text('Open trace · t'),id='mcp-trace')
                            yield Button(Text('Copy call · c'),id='mcp-copy')
                            yield Button(Text('Expand · e'),id='expand-mcp',classes='expand-button')
            with TabPane('4  Traces',id='traces'):
                with Horizontal(classes='split'):
                    yield self.results('traces','trace-table','Filter traces by task, model, service, or ID…','list')
                    with Vertical(id='trace-detail',classes='box'):
                        yield Tree('Select a trace',id='span-tree')
                        yield SelectableRichLog(id='span-info',min_width=1,wrap=True,markup=False,auto_scroll=False)
            with TabPane('5  Host',id='host'):
                yield self.results('host','host-table','Filter processes by name, PID, project, or state…')
                yield SelectableRichLog(id='host-detail',classes='box',min_width=1,wrap=True,markup=False,auto_scroll=False)
            with TabPane('6  Metrics',id='metrics'):
                with Horizontal(classes='split'):
                    yield self.results('metrics','metrics-table','Filter metric, service, kind, or unit…','list')
                    with Vertical(id='metrics-inspector',classes='detail box'):
                        yield Static(id='usage-series')
                        yield SelectableRichLog(id='metrics-detail',classes='detail-log',min_width=1,wrap=True,markup=False,auto_scroll=False)
                        with Horizontal(classes='inspector-actions'):
                            yield Button(Text('Open exemplar · t'),id='metrics-trace',disabled=True)
                            yield Button(Text('Copy metric · c'),id='metrics-copy')
                            yield Button(Text('Expand · e'),id='expand-metrics',classes='expand-button')
            with TabPane('7  Findings',id='findings'):
                with Horizontal(classes='split'):
                    yield self.results('findings','finding-table','Filter findings by severity, type, task, or evidence…','list')
                    with Vertical(id='finding-inspector',classes='detail box'):
                        yield SelectableRichLog(id='finding-detail',classes='detail-log',min_width=1,wrap=True,markup=False,auto_scroll=False)
                        with Horizontal(classes='inspector-actions'):
                            yield Button(Text('Open trace · t'),id='finding-trace')
                            yield Button(Text('Copy finding · c'),id='finding-copy')
                            yield Button(Text('Expand · e'),id='expand-findings',classes='expand-button')
        yield Static('Connecting to receiver…',id='health',markup=False)
        yield Footer()

    def get_default_screen(self):
        return ObservatoryScreen()

    def on_mount(self):
        self.theme_changed_signal.subscribe(self, self.on_theme_changed)
        columns={
            'agent-table':[('Agent',11,0),('Session ID',10,0),('Repo / project',14,0),('Status',14,0),('Updated',8,0),('Task / details',32,1)],
            'agent-actions':[('Time',8,0),('Type',7,0),('Recent actions',15,1)],
            'activity-table':[('Time',8,0),('Type',7,0),('Result',9,0),('Action',20,1)],
            'mcp-servers':[('Server · Enter filters operations',24,3),('Inventory',22,2),('Operations',8,0),('Metric calls',11,0),('Tools',8,0),('Errors',8,0)],
            'mcp-table':[('Time',8,0),('Agent',11,1),('Server',9,1),('Tool / method',14,3),('Result',7,0),('Latency',7,0)],
            'trace-table':[('Result',11,0),('Task / run',20,1),('Spans',5,0),('Tokens in / out',15,0)],
            'host-table':[('Process',10,1),('PID',7,0),('Project',12,2),('State',8,1),('RAM',9,0),('CPU total',9,0)],
            'metrics-table':[('Instrument',20,2),('Service',12,1),('Kind',12,0),('Latest value',17,1),('Unit',8,0),('Points',7,0)],
            'finding-table':[('Severity',9,0),('Kind',18,1),('Finding',32,2),('Occurrences',12,0),('Last seen',10,0)]}
        for name,cols in columns.items():
            self.query_one('#'+name,ResultsTable).configure(cols)
        for name,title in [('results-agents','Agent sessions'),('agent-detail','Session overview'),('agent-actions','Recent work'),('results-activity','Activity stream'),('activity-inspector','Event detail'),('mcp-servers','Configured servers and servers with observed calls'),('results-mcp','MCP operations'),('mcp-inspector','Operation detail'),('results-traces','Traces'),('trace-detail','Execution tree'),('results-host','Agent processes'),('host-detail','Collection status'),('results-metrics','OTLP metric instruments'),('metrics-inspector','Metric detail'),('results-findings','Finding review'),('finding-inspector','Evidence and source')]:
            self.query_one('#'+name).border_title=title
        for page in self.search_queries:
            self.query_one('#results-'+page).border_subtitle='↑↓ select  ·  Enter inspect'
            self.query_one('#search-'+page).tooltip='Search this view · / to focus · Esc to return'
        self.query_one('#reviews').tooltip='Include internal approval review sessions'
        self.query_one('#pause').tooltip='Pause display updates · collection continues'
        for log in self.query(SelectableRichLog):
            log.tooltip='Drag to select text · Ctrl+C copies selection · c copies details'
        self.query_one('#host-detail').styles.height=8
        self.update_layout()
        if not self.feed.thread.is_alive():self.feed.thread.start()
        self.refresh_timer=self.set_interval(.5,self.refresh_data)
        self.query_one('#agent-table',DataTable).focus()

    def on_unmount(self):
        if self.refresh_timer:self.refresh_timer.stop()
        for timer in self.search_timers.values():timer.stop()

    def on_theme_changed(self, theme):
        # RichLog stores rendered strips; cached rows/inspectors must be rebuilt too.
        self.table_signatures.clear()
        self.span_signature=None
        self.update_layout()
        self.reflow_inspectors()

    def on_resize(self,event):
        self.update_layout(event.size)
        self.call_after_refresh(self.reflow_inspectors)

    def reflow_inspectors(self):
        self.screen.clear_selection()
        positions={log.id:log.scroll_offset for log in self.query(SelectableRichLog)}
        self.inspector_signatures.clear()
        self.last_update=-1
        self.refresh_data()
        trees=self.query('#span-tree')
        if trees and trees.first().cursor_node:
            self.show_span(trees.first().cursor_node.data)
        def restore():
            for name,position in positions.items():
                self.query_one('#'+name).scroll_to(x=position.x,y=position.y,animate=False)
        self.call_after_refresh(restore)

    def update_layout(self,size=None):
        size=size or self.size
        self.screen.set_class(size.width<140,'narrow')
        self.screen.set_class(size.width<100,'small')
        self.screen.set_class(size.height<36,'compact')
        for tower in self.query('#tower'):
            tower.update(themed_text(tower,tower_mark(compact=size.height<36)))
        for brand in self.query('#brand'):
            brand.update(themed_text(brand,masthead(demo=getattr(self,'demo_mode',False),compact=size.height<36 or size.width<100)))
        self.last_update=-1

    def stat(self,name,value,label,detail):
        accent={'sessions':AMBER,'processes':MINT,'mcp-count':LAVENDER,'usage':ICE}[name]
        text=Text(label.upper()+'\n',style='#b9afc5',no_wrap=True,overflow='ellipsis')
        text.append(str(value),style='bold '+accent)
        text.append('\n'+detail,style='#b9afc5')
        self.query_one('#stat-'+name,Static).update(themed_text(self.query_one('#stat-'+name),text))

    def fill(self,name,rows,preserve=None):
        """Preserve row identity and scroll position while live values change."""
        signature=tuple((key,tuple(str(value) for value in values)) for key,values,record in rows)
        self.table_data[name]={key:record for key,values,record in rows}
        if self.table_signatures.get(name)==signature:return
        self.table_signatures[name]=signature
        table=self.query_one('#'+name,DataTable)
        rows=[(key,[themed_text(table,value) if isinstance(value,Text) else value for value in values],record)
              for key,values,record in rows]
        selected=preserve
        if table.row_count:
            try:
                if not selected:selected=table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            except Exception:pass
        scroll=table.scroll_offset
        old_index=table.cursor_row
        with table.prevent(DataTable.RowHighlighted):
            if ([key.value for key in table.rows]==[key for key,_,_ in rows] and
                all(table.rows[key].height==table.preview_height(values) for key,values,_ in rows)):
                for key,values,_ in rows:
                    for column,value in zip(table.columns,values):
                        previous=table.get_cell(key,column)
                        # Rich Text equality excludes its base style; a theme
                        # change must still recolor otherwise identical cells.
                        if previous!=value or getattr(previous,'style',None)!=getattr(value,'style',None):
                            table.update_cell(key,column,value)
            else:
                table.clear()
                for key,values,record in rows:table.add_row(*values,key=key)
            if rows:
                index=next((index for index,(key,_,__) in enumerate(rows) if key==selected),0)
                table.move_cursor(row=index,scroll=False)
                table.scroll_to(x=scroll.x,y=max(0,scroll.y+index-old_index),animate=False)

    def search_status(self,page,count,noun,plural=None):
        query=self.search_queries[page]
        waiting=page in {'activity','mcp','traces'} and self.data.queries.get(page,'')!=query
        text='Filtering…' if waiting else quantity(count,('matching '+noun) if query else noun,
                                                    ('matching '+plural) if query and plural else plural)
        if not waiting and query and not count:text+=' · Clear to remove this filter'
        if page=='activity' and self.prompts_only:text+=' · user prompts only'
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
        state='Paused' if self.feed.paused else 'Reconnecting' if data.error else 'Connected'
        state_color='#ffbd69' if self.feed.paused else '#ff8799' if data.error else '#86e3b5'
        endpoint=self.feed.api.endpoint.removeprefix('http://').removeprefix('https://')
        connection_widget=self.query_one('#connection',Static)
        connection_widget.tooltip=state+' · '+endpoint
        connection=Text()
        connection.append('● ', style='bold '+state_color)
        connection.append(state, style='bold '+state_color)
        if self.size.width>=120:
            connection.append('  '+endpoint, style='#b9afc5')
        connection_widget.update(themed_text(connection_widget,connection))
        health_text=(data.error if data.error else
            f"{'Display paused' if self.feed.paused else 'Updated '+age(int(data.updated*1e9))}  ·  {self.scope_label()}  ·  {'Reviews included' if self.feed.include_reviews else 'Reviews hidden'}")
        coverage=coverage_warning(data.local_status)
        if coverage:health_text+='  ·  '+coverage
        health=Text(health_text,style='#ffbd69' if coverage and not data.error else '#ff8799' if data.error else '#b9afc5')
        health_widget=self.query_one('#health',Static)
        health_widget.update(themed_text(health_widget,health))
        if self.screen.selections:
            self.query_one('#health',Static).update('Text selected · Ctrl+C to copy · click to clear selection and resume display updates')
            return
        if data.updated==self.last_update:return
        self.last_update=data.updated;self.data=data
        agents=sources(data,self.feed.include_reviews)
        work=sources(data,False)
        recent=sum(item['recent'] for item in work)
        reviews=sum(entry.get('service')=='codex-review' for entry in data.local_status.get('sessions',[]))
        processes=(data.local_status.get('runtime') or {}).get('processes',[])
        live=sum(not str(item.get('state','')).startswith(('Z','X')) for item in processes)
        waiting=sum(item['state']=='Waiting' for item in work)
        replies=sum(input_required(item) for item in work)
        session_detail=f'{recent} recent · {waiting} waiting · {replies} action'
        stat_width=(self.size.width-(4 if self.size.width>=140 else 2))//(2 if self.size.width<100 else 4)
        if cell_len(session_detail)>stat_width-(5 if self.size.width>=140 else 3):
            session_detail=f'{waiting} waiting · {replies} action'
        self.stat('sessions',len(work),'Agent sessions',session_detail)
        self.stat('processes',live,'Host processes','Live on this host')
        servers={item.get('server') for item in data.mcp if item.get('server') and item.get('server')!='Not reported'}
        operations={(item.get('server'),item.get('tool')) for item in data.mcp if item.get('tool') and item.get('tool')!='Not reported'}
        mcp_errors=sum(str(item.get('status','')).upper() in {'ERROR','FAILED'} for item in data.mcp)
        inventory=data.mcp_summary
        call_count=inventory.get('calls',len(data.mcp))
        server_count=len(inventory['servers']) if inventory else len(servers)
        tool_count=inventory.get('tools',len(operations))
        mcp_errors=inventory.get('errors',mcp_errors)
        self.stat('mcp-count',number(call_count),'MCP calls',
                  f'{quantity(server_count,"server")} · {quantity(mcp_errors,"error")}')
        self.query_one('#stat-mcp-count').tooltip=f'{quantity(tool_count,"tool")} · All stored calls in the selected scope. Configured servers appear only after an observed call.\n'+', '.join(row['server'] for row in inventory.get('servers',[]))
        if inventory.get('metric_calls'):
            self.query_one('#stat-mcp-count').tooltip += (
                f"\nCursor aggregate MCP metric: {number(inventory['metric_calls'])} calls across all conversations; "
                "shown separately because metrics have no session IDs or call details.")
        self.query_one('#mcp-servers').tooltip=(
            'Calls are individual calls with operation details. Metric calls are Cursor aggregate counts; '
            'they cannot be opened or attributed to a session. Calls without a reported server are listed separately.')
        self.stat('usage',observed_count(data.totals,'input_tokens')+' / '+observed_count(data.totals,'output_tokens'),'Tokens in / out','Includes cached input')
        self.query_one('#stat-sessions').tooltip=f'{recent} updated recently · {waiting} waiting · {replies} need action\n{reviews} approval reviews · Recent means a session record changed within 35 seconds'
        self.query_one('#stat-usage').tooltip=(
            'Recorded token totals; repeated and cached input is included. '
            f"Cache read {observed_count(data.totals,'cache_read_input_tokens')} · "
            f"cache write {observed_count(data.totals,'cache_write_input_tokens')} · "
            f"reasoning output {observed_count(data.totals,'reasoning_output_tokens')}. "
            'Breakdowns are already included in headline totals. This is not a billable cost estimate.')
        options=[('All agent sessions','all')]+[(f"{item['runtime']} · {item.get('project') or 'SDK'} · {(item.get('session_id') or item['id'])[-6:]}",item['id']) for item in agents]
        if options!=self.scope_options:
            picker=self.query_one('#scope',Select);old=self.scope_value
            picker.set_options(options);picker.value=old if any(value==old for _,value in options) else 'all';self.scope_options=options
        self.source_map={item['id']:item for item in agents}
        agent_query=self.search_queries['agents'].casefold()
        visible_agents=[item for item in agents if (self.scope_value=='all' or item['id']==self.scope_value) and
                        (not agent_query or agent_query in ' '.join(str(item.get(key) or '') for key in ('runtime','service','project','working_directory','branch','task','model','session_id','id','state','waiting_for')).casefold())]
        visible_agents.sort(key=lambda item:(not input_required(item),not item['recent'],-item['stamp'],item['id']))
        self.fill('agent-table',[(item['id'],[
            cell(item['runtime']),cell(item.get('session_id') or item['id'],'#c5adff'),
            cell(item.get('project') or '—'),
            cell(input_status(item),'bold #ff8799' if input_required(item) else '#ffbd69' if item['state']=='Waiting' else '#86e3b5' if item['recent'] else '#b9afc5'),
            cell(age(item['stamp']),'#b9afc5'),task_text(item)],item) for item in visible_agents])
        self.search_status('agents',len(visible_agents),'session')
        if self.selected_source not in {item['id'] for item in visible_agents}:self.selected_source=visible_agents[0]['id'] if visible_agents else None
        self.show_agent()
        activity_items=self.visible_results('activity',data.activity_results if data.activity_results is not None else data.activity)
        activity_items=self.hold_selected('activity',activity_items,self.selected_activity,'source_id')
        activity=[(item.get('id') or item['source_id'],[cell(table_when(item['time_unix_nano'])),cell(item['domain'].upper(),'#c5adff'),status(item.get('result')),cell(headline(item))],item) for item in activity_items]
        self.query_one('#results-activity').border_title='User prompts · newest first' if self.prompts_only else 'Activity stream'
        self.fill('activity-table',activity)
        self.search_status('activity',len(activity),'prompt' if self.prompts_only else 'event')
        if self.selected_activity not in self.table_data.get('activity-table',{}):self.selected_activity=activity[0][0] if activity else None
        self.show_activity()
        observed={row['server']:row for row in data.mcp_summary.get('servers',[])}
        metric_observed={row['server']:row for row in data.mcp_summary.get('metric_servers',[])}
        configured={}
        for item in data.local_status.get('mcp_configurations',[]):
            configured.setdefault(item['server'],[]).append(item)
        inventory=[]
        for server in sorted(set(observed)|set(metric_observed)|set(configured),key=str.casefold):
            calls=observed.get(server,{'calls':0,'tools':0,'errors':0})
            metric_calls=metric_observed.get(server,{}).get('calls',0)
            configs=configured.get(server,[])
            origins=', '.join(sorted({item['client']+' ('+item['scope']+')' for item in configs}))
            label=('Configured · '+origins if configs else
                   'Aggregate metric · Cursor' if metric_calls and server not in observed else 'Observed')
            if configs and server in observed:
                label='Observed · configured in '+origins
            if metric_calls and (configs or server in observed):
                label+=' · metric totals available'
            row={'server':server,'calls':calls.get('calls',0),'metric_calls':metric_calls,
                 'tools':calls.get('tools',0),'errors':calls.get('errors',0)}
            inventory.append((server,[cell(server),cell(label,'#ffbd69' if configs else '#b9afc5'),
                cell(number(row['calls'])),cell(number(row['metric_calls'])),
                cell(number(row['tools'])),cell(number(row['errors']))],row))
        unknown_calls=data.mcp_summary.get('unknown_server_calls',0)
        if unknown_calls:
            unknown={'server':'Not reported','calls':unknown_calls,
                     'metric_calls':0,'tools':data.mcp_summary.get('unknown_server_tools',0),
                     'errors':data.mcp_summary.get('unknown_server_errors',0)}
            inventory.append(('Not reported',[cell('Not reported'),
                cell('Server identity missing · attribution unavailable','#ffbd69'),
                cell(number(unknown['calls'])),cell('0'),cell(number(unknown['tools'])),
                cell(number(unknown['errors']))],unknown))
        self.fill('mcp-servers',inventory)
        mcp_items=self.visible_results('mcp',data.mcp_results if data.mcp_results is not None else data.mcp)
        mcp_items=self.hold_selected('mcp',mcp_items,self.selected_mcp,'id')
        self.fill('mcp-table',[(item['id'],[cell(table_when(item['time_unix_nano']),'#b9afc5'),cell((item.get('service') or 'Unknown')+' · '+str(item.get('session_id') or '—')[-6:]),cell(item['server'],'#c5adff'),cell(item['tool'] if item.get('tool') not in ('', 'Not reported') else item.get('method') or 'Not reported'),status(item['status']),cell('—' if item['duration_ms'] is None else timing(item['duration_ms']))],item) for item in mcp_items])
        if self.selected_mcp not in self.table_data.get('mcp-table',{}):self.selected_mcp=mcp_items[0]['id'] if mcp_items else None
        self.show_mcp()
        if not data.mcp_available:
            mcp_hint='This receiver version does not expose MCP operations. Update and restart the receiver.'
        elif data.queries.get('mcp','')!=self.search_queries['mcp']:
            mcp_hint='Filtering MCP operations…'
        elif mcp_items:
            server_count=len({item.get('server') for item in mcp_items if item.get('server') and item['server']!='Not reported'})
            error_count=sum(str(item.get('status','')).upper() in {'ERROR','FAILED'} for item in mcp_items)
            mcp_hint=f"{quantity(len(mcp_items),'matching operation' if self.search_queries['mcp'] else 'operation')} · {quantity(server_count,'server')} · {quantity(error_count,'error')}"
        elif self.search_queries['mcp']:
            mcp_hint='No matching MCP operations · Clear to show all calls in this scope'
        elif data.mcp_summary.get('metric_calls'):
            mcp_hint=(f"No per-call events · {quantity(data.mcp_summary['metric_calls'],'aggregate MCP metric call')} "
                      "from Cursor; select Metrics for the underlying series")
        else:
            mcp_hint='No MCP operations in this scope · General tool activity is available in Activity'
        if 'mcp' in self.errors_only:mcp_hint=f"{quantity(len(mcp_items),'failed call')} · errors only · current results"
        config_errors=data.local_status.get('mcp_config_errors',0)
        if config_errors:
            mcp_hint+=f" · {quantity(config_errors,'MCP config could not be read')}"
        self.query_one('#mcp-hint',Static).update(mcp_hint)
        traces=self.visible_results('traces',data.traces[:])
        detail=data.detail
        if self.feed.pinned_trace and detail.get('summary') and not any(item['trace_id']==detail['trace_id'] for item in traces):traces.insert(0,detail['summary'])
        self.fill('trace-table',[(item['trace_id'],[cell('Active · errors','yellow') if item.get('active') and item['status']=='ERROR' else cell('Error','red') if item['status']=='ERROR' else status(item['status']),cell(trace_name(item)),cell(item['span_count']),cell(observed_count(item,'input_tokens')+' / '+observed_count(item,'output_tokens'))],item) for item in traces],preserve=self.feed.selected_trace)
        self.search_status('traces',len(traces),'trace')
        finding_query=self.search_queries['findings'].casefold()
        finding_items=[item for item in data.finding_results if not finding_query or finding_query in ' '.join(
            str(item.get(key) or '') for key in ('severity','kind','title','detail','task','session_id','services','run_status')).casefold()]
        self.fill('finding-table',[(item['id'],[
            cell(str(item.get('severity') or 'Unknown').upper(),'#ff8799' if item.get('severity')=='high' else '#ffbd69' if item.get('severity')=='medium' else '#c5adff'),
            cell(item.get('kind') or 'Finding'),cell(item.get('title') or 'Review finding'),
            cell(number(item.get('occurrence_count',1))),cell(age(item.get('time_unix_nano',0)) if item.get('time_unix_nano') else '—')],item)
            for item in finding_items])
        self.search_status('findings',len(finding_items),'finding')
        if self.selected_finding not in self.table_data.get('finding-table',{}):
            self.selected_finding=finding_items[0]['id'] if finding_items else None
        self.show_finding()
        for page in ('activity','mcp','traces','findings'):
            paging=data.pagination.get(page,{})
            page_number=paging.get('page',1)
            self.query_one('#history-label-'+page,Static).update('Latest' if page_number==1 else f'History · page {page_number}')
            self.query_one('#older-'+page,Button).disabled=not paging.get('next_cursor')
            self.query_one('#newer-'+page,Button).disabled=page_number==1
            self.query_one('#latest-'+page,Button).disabled=page_number==1
            self.query_one('#older-'+page).tooltip='Load older saved records · ]' if paging.get('supported') else 'Restart the updated receiver to enable history paging'
        self.show_spans()
        host_query=self.search_queries['host'].casefold()
        visible_processes=[item for item in processes if not host_query or host_query in ' '.join(str(item.get(key) or '') for key in ('name','pid','parent_pid','project','state')).casefold()]
        self.fill('host-table',[(str(item['pid']),[cell(item['name']),cell(item['pid']),cell(item.get('project') or '—'),cell(item['state']),cell(f"{item['memory_rss_kib']/1024:.0f} MiB"),cell(f"{item['cpu_seconds']:.1f} s")],item) for item in visible_processes])
        self.search_status('host',len(visible_processes),'process','processes')
        runtime=data.local_status.get('runtime') or {}
        opencode_sessions=sum(entry.get('runtime')=='OpenCode' for entry in data.local_status.get('sessions',[]))
        self.inspect('host-detail','Host & collection status',[
            ('Host',runtime.get('host','Not reported')),('Platform',str(runtime.get('os',''))+' / '+str(runtime.get('architecture',''))),
            ('Load average',', '.join(str(value) for value in runtime.get('load_average',[])) or 'Unavailable'),
            ('Sources',f"{data.local_status.get('sources',0)} Codex / Claude files · {opencode_sessions} OpenCode sessions · lookback {data.local_status.get('lookback_hours',24)}h · read errors {data.local_status.get('read_errors',0)+data.local_status.get('opencode_read_errors',0)}"),
            ('Coverage','Source logs can be partial. Costs and model-call latency are unavailable unless reported. CPU is cumulative process time.')],[])
        metric_query=self.search_queries['metrics'].casefold()
        selected_source=getattr(self,'source_map',{}).get(self.scope_value)
        scoped_service=selected_source.get('service') if selected_source else None
        visible_catalog=[]
        for item in data.metric_catalog:
            if scoped_service and item.get('service')!=scoped_service:
                continue
            searchable=' '.join(str(item.get(key) or '') for key in
                ('name','service','kind','unit','description','scope_name')).casefold()
            if metric_query and metric_query not in searchable:
                continue
            visible_catalog.append(item)
        def metric_key(item):
            return json.dumps([item.get(key) for key in
                ('service','name','kind','temporality','scope_name','unit','description')],ensure_ascii=False)
        latest_by_key={}
        for point in data.metric_points:
            latest_by_key.setdefault(metric_key(point),point)
        metric_rows=[]
        for item in visible_catalog:
            latest=latest_by_key.get(metric_key(item))
            metric_rows.append((metric_key(item),[
                cell(item.get('name') or 'unnamed'),cell(item.get('service') or 'unknown'),
                cell(item.get('kind') or '—'),cell(metric_value(latest) if latest else 'No point'),
                cell(item.get('unit') or '—'),cell(number(item.get('point_count') or 0))],item))
        self.fill('metrics-table',metric_rows,preserve=self.selected_metric)
        if self.selected_metric not in self.table_data.get('metrics-table',{}):
            self.selected_metric=metric_rows[0][0] if metric_rows else None
        metric=self.table_data.get('metrics-table',{}).get(self.selected_metric)
        if metric:
            selected_metric_key=metric_key(metric)
        else:
            selected_metric_key=None
            self.metric_history_requested_key=None
            self.metric_history_loaded_key=None
            self.metric_history_points=[]
        if not data.metrics_available:
            metric_hint='This receiver version does not expose OTLP metrics. Update and restart the receiver.'
        elif metric_query and not metric_rows:
            metric_hint='No matching metric instruments · Clear to show the catalog.'
        elif metric_rows:
            metric_hint=quantity(len(metric_rows),'instrument')+' · latest readings from stored OTLP points'
        else:
            metric_hint='No OTLP metrics recorded in this scope.'
        self.query_one('#search-status-metrics',Static).update(metric_hint)
        self.query_one('#usage-series',Static).update(usage_series_text(
            data.usage_series,data.usage_service,data.usage_session_id))
        if metric:
            history=self.metric_history_points if self.metric_history_loaded_key==selected_metric_key else []
            latest=history[0] if history else latest_by_key.get(metric_key(metric))
            exemplars=[]
            for point in history or ([latest] if latest else []):
                exemplars.extend((point.get('data') or {}).get('exemplars') or [])
            exemplar=next((item for item in exemplars if item.get('trace_id')),None)
            self.query_one('#metrics-trace',Button).disabled=exemplar is None
            point_fields=[]
            if latest:
                temporality=('Cumulative (implicit for Summary)' if latest.get('kind')=='summary' else
                    {0:'Unspecified',1:'Delta',2:'Cumulative'}.get(latest.get('temporality'),str(latest.get('temporality'))))
                point_fields=[('Latest reading',metric_value(latest)),('Observed',age(latest.get('time_unix_nano'))),
                    ('Temporality',temporality),
                    ('Point start',when(latest.get('start_unix_nano')) if latest.get('start_unix_nano') else 'Not reported')]
                monotonic=(latest.get('data') or {}).get('isMonotonic')
                if latest.get('kind')=='sum' and isinstance(monotonic,bool):
                    point_fields.append(('Semantics','Monotonic counter' if monotonic else 'Up/down sum'))
            history_lines=[]
            for point in history[:12]:
                stamp=point.get('time_unix_nano')
                label=when(stamp) if stamp else 'Unknown time'
                attrs=point.get('attributes') or {}
                suffix=' · '+json.dumps(attrs,ensure_ascii=False,sort_keys=True) if attrs else ''
                history_lines.append(f"{label}  {metric_value(point)} {metric.get('unit') or ''}{suffix}".rstrip())
            self.inspect('metrics-detail','OTLP metric instrument',[
                ('Name',metric.get('name')),('Service',metric.get('service')),('Kind',metric.get('kind')),
                ('Unit',metric.get('unit') or 'Unitless'),('Description',metric.get('description') or 'Not reported'),
                ('Instrumentation scope',metric.get('scope_name') or 'Not reported'),
                ('Stored points',metric.get('point_count',0)),*point_fields],
                [('Latest point attributes',latest.get('attributes') if latest else None),
                 ('Resource attributes',latest.get('resource_attributes') if latest else None),
                 ('Recent points · newest first','\n'.join(history_lines) if history_lines else None),
                 ('Point data',latest.get('data') if latest else None)])
        else:
            self.query_one('#metrics-trace',Button).disabled=True
            self.inspect('metrics-detail','OTLP metric instruments',[('Status',metric_hint)],[])

    def scope_label(self):
        item=getattr(self,'source_map',{}).get(self.scope_value)
        return f"{item['runtime']} / {item.get('project') or 'SDK'}" if item else 'all agent sessions'

    def hold_selected(self,page,items,selected,key):
        """Keep the event being read when new arrivals push it off the live page."""
        current=self.table_data.get(page+'-table',{}).get(selected)
        if current and selected not in {item.get('id') or item[key] for item in items}:
            return [*items,current]
        return items

    def inspect(self,name,title,fields,sections):
        signature=(title,str(fields),str(sections))
        if self.inspector_signatures.get(name)==signature:return
        previous=self.inspector_signatures.get(name)
        self.inspector_signatures[name]=signature
        log=self.query_one('#'+name,RichLog)
        scroll=log.scroll_offset
        log.clear();log.write(Text(title,style='bold #f6f0e8'))
        table=Table.grid(padding=(0,2));table.add_column(style=themed_style(log,'#b9afc5'),no_wrap=True);table.add_column()
        for label,value in fields:table.add_row(Text(label),Text(str('Not reported' if value is None or value=='' else value)))
        log.write(table)
        for label,value in sections:self.write_evidence(log,label,value)
        if previous and previous[0]==title and previous[2]==signature[2]:
            log.scroll_to(y=scroll.y,animate=False)

    def write_evidence(self,log,label,value,depth=0):
        if value is None or value=='':return
        value=decode_nested(value)
        if isinstance(value,str) and label.casefold() in {'recorded content','text','output','response'}:
            segments=embedded_json(value)
            if any(kind=='json' for kind,_ in segments):
                log.write(Text('\n'+label.title(),style='bold #c5adff'))
                for kind,content in segments:
                    if kind=='text':
                        if content.strip():log.write(Text(content.strip('\n')))
                    else:
                        write_json(log,content,self.theme)
                return
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
            log.write(Text('\n'+label.upper(),style='bold #ffbd69'))
            if label.lower()=='arguments':
                write_json(log,value,self.theme)
                return
        if isinstance(value,(dict,list)) and not value:
            log.write(Text('\n'+label.title(),style='bold #c5adff'))
            log.write(Text('No entries' if isinstance(value,list) else 'Empty object'))
            return
        if isinstance(value,dict) and value.get('type') in {'text','input_text','output_text'} and 'text' in value:
            self.write_evidence(log,label,value['text'],depth+1);return
        # Any structured value nested in captured evidence is easier to scan
        # as JSON than as a sequence of flattened label/value rows. This also
        # handles JSON strings stored under fields such as `output` or `text`.
        if depth and isinstance(value,(dict,list)):
            write_json(log,value,self.theme)
            return
        if depth<64 and isinstance(value,dict):
            for key,child in value.items():
                self.write_evidence(log,str(key).replace('_',' '),child,depth+1)
            return
        if depth<64 and isinstance(value,list):
            for child in value:self.write_evidence(log,label,child,depth+1)
            return
        if isinstance(value,(dict,list)):
            log.write(Text('\n'+label.title(),style='bold #c5adff'))
            write_json(log,value,self.theme)
            return
        log.write(Text('\n'+label.title(),style='bold #c5adff'))
        if label.lower()=='command':
            command=shell_command(value)
            script=heredoc_script(command)
            if script:
                language,title,body=script
                log.write(Text(title,style='bold #c5adff'))
                log.write(evidence_syntax(body or '# Empty script',language,self.theme))
                return
            log.write(evidence_syntax(command,'bash',self.theme))
        elif label.lower() in {'diff','patch','unified diff','code'}:
            lexer='diff' if 'diff' in label.lower() or label.lower()=='patch' else 'python' if label.lower()=='code' else 'bash'
            log.write(evidence_syntax(str(value),lexer,self.theme))
        else:log.write(Text(str(value)))

    def show_agent(self):
        item=getattr(self,'source_map',{}).get(self.selected_source)
        button=self.query_one('#agent-summary',Button)
        button.disabled=not bool(item) or self.summary_busy or not self.ai_summary_available
        if not self.ai_summary_available:
            button.tooltip='No compatible API key or provider is configured. The recorded progress summary below works without setup.'
        if not item:
            self.query_one('#agent-open',Button).disabled=True
            self.fill('agent-actions',[('__empty__',[cell('—'),cell('INFO'),cell('No session selected')],{})])
            if self.search_queries['agents']:
                self.inspect('agent-detail','No matching agent sessions',[],[('Search','Clear the filter in Agent sessions to show all sessions in this scope.')])
            else:
                self.inspect('agent-detail','No agent sessions found',[],[('Next step','Start or continue an agent in its own terminal. This cockpit monitors existing sessions.')])
            return
        self.query_one('#agent-open',Button).disabled=False
        summary=next((entry for entry in self.data.sessions if item.get('session_id') and entry.get('session_id')==item.get('session_id')),{})
        latest=item.get('last_activity') or {}
        generated=self.ai_summaries.get(item['id'])
        ai_sections=[]
        if generated:
            stamp,text=generated
            label='AI summary · earlier snapshot' if stamp!=item.get('stamp') else 'AI summary · provider generated'
            ai_sections=[(label,text)]
        actions=[event for event in self.data.activity if event.get('session_id')==item.get('session_id')] if item.get('session_id') else [event for event in self.data.activity if event.get('service')==item.get('service')]
        meaningful=[event for event in actions if event.get('domain') in {'shell','file','mcp','browser','model','message'}]
        actions=sorted(meaningful or actions,key=lambda event:event.get('time_unix_nano',0),reverse=True)
        progress=recorded_progress(item,actions,summary)
        wait=wait_summary(item)
        work_detail=(f"{reported_number(summary.get('trace_count'))} traces · {reported_number(item.get('spans'))} spans · "
                     f"{observed_count(summary,'input_tokens')} in / {observed_count(summary,'output_tokens')} out")
        token_detail=(f"Cache read {observed_count(summary,'cache_read_input_tokens')} · "
                      f"cache write {observed_count(summary,'cache_write_input_tokens')} · "
                      f"reasoning output {observed_count(summary,'reasoning_output_tokens')}")
        cost_detail=('Not reported' if not summary or summary.get('unknown_cost_runs')
                     else f"${summary.get('known_cost_usd',0):.4f} known")
        session_details=(f"Session ID: {item.get('session_id') or item['id']}\n"
            f"Repo / project: {item.get('project') or 'Not reported'}\n"
            f"Branch: {item.get('branch') or 'Not reported'}\n"
            f"Model: {item.get('model') or 'Not reported'}\n"
            f"Work: {work_detail}\nToken breakdown: {token_detail}\nCost: {cost_detail}")
        self.inspect('agent-detail',clean(item.get('task') or 'Task not reported'),[
            ('Agent',item['runtime']),
            ('Status',item['state']),('Waiting on',wait if item['state']=='Waiting' or item.get('waiting_for') else 'No wait reported'),
            ('Last updated',when(item['stamp'])+' · '+age(item['stamp']) if item['stamp'] else 'Not reported')],
            [('Progress summary · recorded activity',progress)]+ai_sections+[
             ('Session details',session_details),
             ('Working directory',item.get('working_directory')),
             ('Status evidence','No new session records for 35 seconds; the last reported wait may no longer be current.' if item['state']=='No updates' else 'Status comes from recorded lifecycle events. A tool response wait is not an approval request.')])
        rows=[(event.get('id') or event['source_id'],[cell(table_when(event['time_unix_nano'])),cell(event['domain'].upper()),cell(headline(event))],event) for event in actions[:30]]
        if not rows:
            rows=[('__empty__',[cell('—'),cell('INFO'),cell('No recent activity captured')],{})]
        self.fill('agent-actions',rows)

    def show_activity(self):
        item=self.table_data.get('activity-table',{}).get(self.selected_activity)
        self.query_one('#activity-trace',Button).disabled=not bool(item and item.get('trace_id'))
        if not item:
            if 'activity' in self.errors_only:
                self.inspect('activity-detail','No failed events',[],[('Errors filter','No failures in the current results. Turn off Errors to see all activity.')]);return
            if self.prompts_only:
                self.inspect('activity-detail','No user prompts in this scope',[],[('Prompts','No user prompts were recorded for this agent in the selected history. Choose another session or turn off Prompts to see all activity.')]);return
            self.inspect('activity-detail','No activity in this scope',[],[('Scope','Choose another agent session or clear the search to view recent activity.')]);return
        details=decode(item.get('detail') or '')
        sections=list(details.items()) if isinstance(details,dict) else [('Recorded content',details)]
        if item.get('metadata'):
            sections.append(('Runtime metadata',item['metadata']))
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
        operation=item.get('tool') if item.get('tool') not in ('', 'Not reported') else item.get('method') or 'MCP operation'
        self.inspect('mcp-detail',item['server']+' / '+operation,[
            ('Result',str(status(item.get('status')))+' · '+timing(item.get('duration_ms'))),
            ('Agent',(item.get('service') or 'Not reported')+' · '+str(item.get('session_id') or 'Unknown session')[-12:]),
            ('Method',item.get('method') or 'Not reported'),('Observed',when(item.get('time_unix_nano') or 0))],
            [('Arguments',item.get('arguments')),('Response',item.get('output')),
             ('Runtime metadata',item.get('metadata')),
             ('Evidence',item.get('source')),('Session ID',item.get('session_id')),
             ('Trace ID',item.get('trace_id') or 'Not linked'),('Span ID',item.get('span_id') or 'Not linked')])

    def show_finding(self):
        item=self.table_data.get('finding-table',{}).get(self.selected_finding)
        self.query_one('#finding-trace',Button).disabled=not bool(item and item.get('trace_id'))
        if not item:
            paging=self.data.pagination.get('findings',{})
            note=('No findings in this page. Use Older to inspect earlier findings.'
                  if paging.get('page',1)>1 else
                  'Recorded security, policy, tool, and run findings appear here with links to their evidence.')
            self.inspect('finding-detail','No findings match this view',[],[('Coverage',note)])
            return
        evidence='\n'.join(f"{entry.get('type','evidence')} · {entry.get('id','not reported')}"
                           for entry in item.get('evidence',[])) or 'No linked evidence was recorded.'
        self.inspect('finding-detail',item.get('title') or 'Review finding',[
            ('Severity',str(item.get('severity') or 'Not reported').upper()),
            ('Type',item.get('kind') or 'Not reported'),
            ('Last observed',when(item.get('time_unix_nano') or 0)),
            ('Occurrences',f"{number(item.get('occurrence_count',1))} across {number(item.get('trace_count',1))} traces · {number(item.get('session_count',0))} sessions"),
            ('Agent / session',(item.get('services') or ['Not reported'])[0]+' · '+str(item.get('session_id') or 'Not reported')),
            ('Task',item.get('task') or 'Not reported')],
            [('What was observed',item.get('detail') or 'No additional detail reported.'),
             ('Evidence',evidence),('Trace ID',item.get('trace_id') or 'Not linked')])

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
                label=Text(f"{kind:5} ",style='#c5adff');label.append(clean(item['name']));label.append(' · '+timing(duration),style='#b9afc5')
                if item['status']=='ERROR':label.append(' · ERROR',style='bold red')
                nodes[item['span_id']]=(nodes.get(parent) or tree.root).add(themed_text(tree,label),data=item,expand=True)
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
        elif name=='activity-table':self.selected_activity=event.row_key.value;self.show_activity()
        elif name=='mcp-table':self.selected_mcp=item['id'];self.show_mcp()
        elif name=='finding-table':self.selected_finding=item['id'];self.show_finding()
        elif name=='metrics-table':self.selected_metric=event.row_key.value;self.last_update=-1;self.refresh_data()
        elif name=='trace-table':
            if self.feed.pinned_trace and self.data.detail.get('trace_id')!=self.feed.selected_trace:return
            if self.feed.selected_trace!=item['trace_id']:
                self.feed.selected_trace=item['trace_id'];self.feed.pinned_trace=True;self.feed.request_refresh()
                self.feed.selected_span_id=None
                self.query_one('#span-tree',Tree).root.set_label(Text('Loading selected trace…'))

    @on(DataTable.RowSelected)
    def row_selected(self,event):
        item=self.valid_row(event)
        if not item:return
        if event.data_table.id=='mcp-servers':
            self.query_one('#search-mcp',Input).value=item['server']
            self.query_one('#mcp-table',DataTable).focus()
            return
        if event.data_table.id!='agent-actions' and self.screen.has_class('compact'):
            self.action_expand()
            return
        if event.data_table.id=='agent-table':
            self.selected_source=item['id'];self.show_agent();self.query_one('#agent-detail',RichLog).focus()
        elif event.data_table.id=='agent-actions':
            self.selected_activity=item.get('id') or item['source_id'];self.action_page('activity');self.show_activity()
        elif event.data_table.id=='activity-table':self.query_one('#activity-detail',RichLog).focus()
        elif event.data_table.id=='mcp-table':self.query_one('#mcp-detail',RichLog).focus()
        elif event.data_table.id=='finding-table':self.query_one('#finding-detail',RichLog).focus()
        elif event.data_table.id=='trace-table':self.query_one('#span-tree',Tree).focus()
        elif event.data_table.id=='host-table':self.query_one('#host-detail',RichLog).focus()
        elif event.data_table.id=='metrics-table':
            key=event.row_key.value
            metric=self.table_data.get('metrics-table',{}).get(key)
            if metric and self.metric_history_loaded_key!=key:self.request_metric_history(metric,key)
            self.query_one('#metrics-detail',RichLog).focus()

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
        self.selected_activity=self.selected_mcp=None
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
        if page=='activity':self.selected_activity=None
        if page=='mcp':self.selected_mcp=None
        if page=='findings':self.selected_finding=None
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
        self.selected_activity=self.selected_mcp=None
        self.feed.include_reviews=event.value;self.feed.selected_trace=None;self.feed.pinned_trace=False;self.feed.request_refresh()
        self.feed.selected_span_id=None

    @on(TabbedContent.TabActivated,'#pages')
    def page_activated(self,event):
        self.screen.set_class(event.pane.has_class('expanded'),'inspecting')

    @on(Button.Pressed)
    def button_pressed(self,event):
        if event.button.id=='pause':self.action_pause()
        elif event.button.id=='history-back':self.action_history_back()
        elif event.button.id.startswith(('older-','newer-','latest-')):
            direction,page=event.button.id.split('-',1)
            self.action_history(direction,page)
        elif event.button.id.startswith('expand-'):self.action_expand()
        elif event.button.id=='prompts-activity':
            self.prompts_only=not self.prompts_only
            self.feed.activity_prompts=self.prompts_only
            self.selected_activity=None
            button=event.button
            button.set_class(self.prompts_only,'-on')
            button.label='Prompts ●' if self.prompts_only else 'Prompts'
            if self.prompts_only and 'activity' in self.errors_only:
                self.errors_only.remove('activity')
                errors=self.query_one('#errors-activity',Button)
                errors.remove_class('-on');errors.label='Errors'
            self.last_update=-1;self.feed.request_refresh();self.refresh_data()
        elif event.button.id.startswith('errors-'):
            page=event.button.id.removeprefix('errors-')
            if page=='activity' and self.prompts_only:
                self.prompts_only=False;self.feed.activity_prompts=False
                prompts=self.query_one('#prompts-activity',Button)
                prompts.remove_class('-on');prompts.label='Prompts'
            if page=='activity':self.selected_activity=None
            if page=='mcp':self.selected_mcp=None
            if page in self.errors_only:self.errors_only.remove(page)
            else:self.errors_only.add(page)
            event.button.set_class(page in self.errors_only,'-on')
            event.button.label='Errors ●' if page in self.errors_only else 'Errors'
            self.last_update=-1;self.refresh_data()
        elif event.button.id.startswith('clear-'):
            field=self.query_one('#search-'+event.button.id.removeprefix('clear-'),Input)
            field.value='';field.focus()
        elif event.button.id=='agent-open':self.action_focus_agent()
        elif event.button.id=='agent-summary':self.request_ai_summary()
        elif event.button.id in {'activity-trace','mcp-trace','metrics-trace','finding-trace'}:self.action_open_trace()
        elif event.button.id in {'activity-copy','mcp-copy','metrics-copy','finding-copy'}:self.action_copy()

    def request_ai_summary(self):
        from .ai_summary import SummaryConfig, SummaryError
        item=getattr(self,'source_map',{}).get(self.selected_source)
        if not item or self.summary_busy:return
        if item.get('review'):
            self.notify('AI summaries exclude internal approval reviews.',severity='warning');return
        try:
            config=SummaryConfig.from_env()
        except SummaryError as exc:
            self.notify(str(exc),severity='warning',timeout=12);return
        cached=self.ai_summaries.get(item['id'])
        if cached and cached[0]==item.get('stamp'):
            self.notify('Showing the existing summary for this snapshot.');return
        self.summary_busy=True
        self.query_one('#agent-summary',Button).label='Summarizing…'
        self.show_agent()
        aggregate=next((dict(row) for row in self.data.sessions
                        if item.get('session_id') and row.get('session_id')==item.get('session_id')),{})
        summary_item=dict(item)
        summary_item['telemetry_totals']=aggregate
        self.summarize_agent(summary_item,config)

    @work(group='ai-summary')
    async def summarize_agent(self,item,config):
        from .ai_summary import generate_summary, summary_evidence, SummaryError
        try:
            params={'limit':30,'service':item.get('service') or '',
                    'session_id':item.get('session_id') or '', 'include_reviews':0}
            page=await asyncio.to_thread(self.feed.api.get,'/api/activity?'+urlencode(params))
            evidence=summary_evidence(item,page.get('activity',[]))
            result=await asyncio.to_thread(generate_summary,config,evidence)
            self.ai_summaries[item['id']]=(item.get('stamp'),result)
            if len(self.ai_summaries)>50:self.ai_summaries.pop(next(iter(self.ai_summaries)))
        except SummaryError as exc:
            self.notify(str(exc),severity='error',timeout=10)
        except Exception:
            self.notify('Could not load session evidence for the summary. Retry when the receiver is connected.',severity='error')
        finally:
            self.summary_busy=False
            self.query_one('#agent-summary',Button).label='AI summary'
            self.show_agent()

    def action_pause(self):
        self.feed.paused=not self.feed.paused;self.query_one('#pause',Button).label='Resume' if self.feed.paused else 'Pause';self.feed.request_refresh();self.refresh_data()

    def action_search(self):
        page=self.query_one('#pages',TabbedContent).active
        if page in self.search_queries:
            self.collapse(page)
            self.query_one('#search-'+page,Input).focus()
    def action_back(self):
        if self.screen.selections:
            self.screen.clear_selection();return
        page=self.query_one('#pages',TabbedContent).active
        if isinstance(self.focused,Input) or self.query_one('#'+page).has_class('expanded'):
            self.action_page(page)
        elif self.navigation_history:
            self.action_history_back()
        else:self.action_page(page)

    def remember_location(self):
        page=self.query_one('#pages',TabbedContent).active
        self.navigation_history.append({'page':page,'expanded':self.query_one('#'+page).has_class('expanded'),
            'focus':self.focused.id if self.focused else None,
            'scroll':{widget.id:widget.scroll_offset for widget in self.query('#'+page+' RichLog, #'+page+' DataTable')}})
        self.query_one('#history-back',Button).disabled=False

    def action_history_back(self):
        if not self.navigation_history:return
        location=self.navigation_history.pop()
        self.action_page(location['page'])
        if location['expanded']:self.action_expand()
        def restore():
            if location['focus']:self.query_one('#'+location['focus']).focus()
            for name,position in location['scroll'].items():
                self.query_one('#'+name).scroll_to(x=position.x,y=position.y,animate=False)
        self.call_after_refresh(restore)
        self.query_one('#history-back',Button).disabled=not self.navigation_history

    def action_history(self,direction,page=None):
        page=page or self.query_one('#pages',TabbedContent).active
        if page not in self.feed.history:return
        self.screen.clear_selection()
        old=self.selected_activity,self.selected_mcp,self.selected_finding
        if page=='activity':self.selected_activity=None
        if page=='mcp':self.selected_mcp=None
        if page=='findings':self.selected_finding=None
        if not self.feed.navigate_history(page,direction):
            self.selected_activity,self.selected_mcp,self.selected_finding=old
            return
        self.last_update=-1;self.refresh_data()

    def copy_to_clipboard(self,text):
        """Route all copy actions, including search inputs, to the OS clipboard."""
        self._clipboard=text  # Keep Textual's in-app paste buffer in sync.
        if text:self._copy_in_background(text)

    @work(group='clipboard',exclusive=True)
    async def _copy_in_background(self,text):
        method=await write_system_clipboard(text)
        if method:
            self.notify(f'Copied to {method} clipboard.')
        else:
            super().copy_to_clipboard(text)
            self.notify('Copy sent to terminal; clipboard support depends on your terminal.')

    def action_copy(self):
        selected=self.screen.get_selected_text()
        if selected:
            self.copy_to_clipboard(selected);return
        page=self.query_one('#pages',TabbedContent).active
        if page=='mcp':
            item=self.table_data.get('mcp-table',{}).get(self.selected_mcp)
        elif page=='activity':
            item=self.table_data.get('activity-table',{}).get(self.selected_activity)
        elif page=='findings':
            item=self.table_data.get('finding-table',{}).get(self.selected_finding)
            if item:
                self.copy_to_clipboard(json.dumps(item,ensure_ascii=False,indent=2));return
        elif page=='metrics':
            item=self.table_data.get('metrics-table',{}).get(self.selected_metric)
            if item:
                self.copy_to_clipboard(json.dumps(item,ensure_ascii=False,indent=2));return
            self.notify('Select a metric instrument to copy.');return
        else:
            name={'agents':'agent-detail','traces':'span-info','host':'host-detail'}[page]
            self.copy_to_clipboard(self.query_one('#'+name,SelectableRichLog).plain_text)
            return
        if not item:
            self.notify('Select an activity, MCP operation, or finding to copy.')
            return
        self.copy_to_clipboard(copy_event_text(item))
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
        target={'agents':'agent-detail','activity':'activity-detail','mcp':'mcp-detail','traces':'span-tree','host':'host-detail','metrics':'metrics-detail','findings':'finding-detail'}[page]
        self.query_one('#'+target).focus()
        self.notify('Expanded detail · Esc to return',timeout=2)
    def action_focus_agent(self):
        if self.selected_source:
            self.query_one('#scope',Select).value=self.selected_source
            self.action_page('activity')
    def action_reviews(self):self.query_one('#reviews',Checkbox).toggle()
    def action_all(self):
        self.selected_activity=self.selected_mcp=self.selected_finding=None
        self.query_one('#scope',Select).value='all'
        self.errors_only.clear()
        self.prompts_only=False;self.feed.activity_prompts=False
        for button in self.query('.error-filter'):
            button.remove_class('-on');button.label='Errors'
        prompt_button=self.query_one('#prompts-activity',Button)
        prompt_button.remove_class('-on');prompt_button.label='Prompts'
        for page in self.search_queries:self.query_one('#search-'+page,Input).value=''
        self.last_update=-1;self.refresh_data()
    def action_page(self,page):
        self.screen.clear_selection()
        self.collapse(page)
        self.query_one('#pages',TabbedContent).active=page
        target={'agents':'agent-table','activity':'activity-table','mcp':'mcp-table','traces':'trace-table','host':'host-table','metrics':'metrics-table','findings':'finding-table'}[page]
        self.query_one('#'+target).focus()
    def action_open_trace(self):
        page=self.query_one('#pages',TabbedContent).active
        if page=='metrics':
            points=self.metric_history_points or self.data.metric_points
            exemplar=next((exemplar for point in points
                for exemplar in ((point.get('data') or {}).get('exemplars') or [])
                if exemplar.get('trace_id')),None)
            item={'trace_id':exemplar.get('trace_id'),'span_id':exemplar.get('span_id')} if exemplar else None
        elif page in {'activity','mcp','findings'}:
            item=(self.table_data.get('mcp-table',{}).get(self.selected_mcp) if page=='mcp' else
                  self.table_data.get('activity-table',{}).get(self.selected_activity) if page=='activity' else
                  self.table_data.get('finding-table',{}).get(self.selected_finding))
            if page=='findings' and item:
                span_id=next((entry.get('id') for entry in item.get('evidence',[])
                              if entry.get('type')=='span'),None)
                item={**item,'span_id':span_id}
        else:return
        if not item or not item.get('trace_id'):
            self.notify('This action has no linked trace.');return
        self.remember_location()
        self.jump_span=item.get('span_id')
        self.feed.selected_span_id=self.jump_span
        self.feed.selected_trace=item['trace_id'];self.feed.pinned_trace=True;self.feed.request_refresh();self.action_page('traces')

    @work(group='metric-history',exclusive=True,thread=True)
    def load_metric_history(self,metric,key):
        try:
            points=self.feed.metric_history(metric,100)
        except Exception as exc:
            try:self.call_from_thread(self.finish_metric_history,key,None,clean(exc))
            except RuntimeError:pass
            return
        try:self.call_from_thread(self.finish_metric_history,key,points,'')
        except RuntimeError:pass

    def request_metric_history(self,metric,key):
        self.metric_history_requested_key=key
        self.metric_history_points=[]
        self.load_metric_history(metric,key)

    def finish_metric_history(self,key,points,error):
        if self.metric_history_requested_key!=key:return
        if error:
            self.metric_history_loaded_key=key
            self.metric_history_points=[]
            self.notify('Could not load metric history: '+error,severity='warning')
            return
        self.metric_history_points=points or []
        self.metric_history_loaded_key=key
        self.last_update=-1
        self.refresh_data()
