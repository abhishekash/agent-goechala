"""Agent-first terminal UI. Raw evidence lives in scrollable inspectors."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
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
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.strip import Strip
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, RichLog, Select, Static, TabbedContent, TabPane, TextArea, Tree

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


def write_json(log,value):
    """Render indented JSON and move multiline string values into readable blocks."""
    formatted,text_values=format_json(value)
    log.write(Syntax(formatted,'json',word_wrap=True,background_color='#101720'))
    for path,content in text_values:
        log.write(Text(f'\n{path} · {len(content.splitlines())} lines',style='bold #92b9eb'))
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


class SearchInput(Input):
    """Use familiar select-all behavior in dashboard search fields."""
    BINDINGS=[Binding('ctrl+a','select_all','Select all',show=False)]


class SelectableRichLog(RichLog):
    """Selectable evidence with visible highlighting and plain-text extraction."""
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
    TITLE='Agent Observatory'
    AUTO_FOCUS='#control-prompt'
    CSS='''
    Screen { background: #0d1117; color: #dce4ed; }
    Screen.control-mode #summary, Screen.control-mode #controls { display: none; }
    Screen > .screen--selection { background: #315a73; color: #ffffff; }
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
    Input > .input--selection { background: #315a73; color: #ffffff; }
    .clear-search { width: 9; }
    .error-filter { width: 12; margin-right: 1; }
    .error-filter.-on { color: #ff8b92; border: tall #ff8b92; background: #2d2028; }
    .search-status { height: 1; color: #8b9bb0; padding: 0 1; }
    .history-controls { height: 1; margin-top: 1; }
    .history-controls Static { width: 1fr; color: #8b9bb0; }
    .history-controls Button { min-width: 8; width: auto; height: 1; margin-left: 1; border: none; }
    .history-controls Button:focus { background: #203d47; color: #ffffff; }
    #history-back { width: 10; margin-right: 1; }
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
    #mcp-servers { height: 7; margin-bottom: 1; }
    .expanded #mcp-servers { display: none; }
    #agent-table { height: 1fr; min-height: 3; }
    #agent-bottom { height: 14; margin-top: 1; }
    #control-pane { height: 1fr; }
    #control-launch { height: auto; min-height: 10; margin-bottom: 1; padding: 1; }
    #control-prompt { height: 4; margin: 1 0; }
    #control-launch-row { height: 3; }
    #control-runtime { width: 24; margin-right: 1; }
    #control-start { width: 16; }
    #control-status { height: 1; color: #8b9bb0; }
    #control-alert { height: 1; color: #f2c779; text-style: bold; }
    #control-alert.pulse { color: #0d1117; background: #f2c779; }
    #control-workspace { height: 1fr; }
    #control-sessions-box { width: 1fr; margin-right: 1; }
    #control-attention-box { width: 1fr; }
    #control-sessions, #control-requests { height: 1fr; min-height: 4; }
    #control-detail { height: 5; margin-top: 1; }
    #control-answer { height: 2; margin-top: 1; }
    #control-actions { height: 3; margin-top: 1; }
    #control-actions Button { width: 1fr; }
    #agent-info { width: 1fr; margin-right: 1; }
    #agent-detail { width: 100%; height: 1fr; }
    #agent-open { height: 3; width: 1fr; }
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
              Binding('c','copy','Copy'), Binding('alt+left','history_back','Back',show=False),
              Binding('[','history("newer")','Newer',show=False),Binding(']','history("older")','Older',show=False),
              Binding('l','history("latest")','Latest',show=False),
              Binding('p','pause','Pause'),Binding('a','all','Reset',show=False),Binding('v','reviews','Reviews',show=False),
              Binding('b','browser','Web'),Binding('q','quit','Quit'),Binding('escape','back','Back',show=False),
              Binding('s','focus_agent','Focus agent',show=False),Binding('0','page("control")','Control',show=False),
              Binding('1','page("agents")','Agents',show=False),
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
        self.navigation_history=[]
        self.ai_summaries={}
        self.summary_busy=False
        self.control_snapshot={"sessions":[],"attention":[],"available_runtimes":{},"launches":[]}
        self.control_tables={"control-sessions":{},"control-requests":{}}
        self.selected_control_session=''
        self.selected_control_request=''
        self.control_signature=None
        self.control_error=''
        self.control_runtime_options=None

    def results(self,page,table,placeholder,classes=''):
        """Keep a view's filter, result count, and rows in one panel."""
        controls=[SearchInput(placeholder=placeholder,id='search-'+page,classes='view-search')]
        if page in {'activity','mcp'}:
            controls.append(Button('Errors',id='errors-'+page,classes='error-filter',tooltip='Show only failed calls in the current results'))
        controls.append(Button('Clear',id='clear-'+page,classes='clear-search',disabled=True))
        footer=[]
        if page in {'activity','mcp','traces'}:
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
            yield Static('◈  OBSERVATORY',id='brand')
            yield Static('○ Connecting',id='connection',markup=False)
        with Horizontal(id='summary'):
            for name in ('sessions','processes','mcp-count','usage'):
                yield Static('',id='stat-'+name,classes='stat',markup=False)
        with Horizontal(id='controls'):
            yield Button('← Back',id='history-back',disabled=True,tooltip='Return to the event you opened · Alt+Left')
            yield Static('Scope',id='scope-label')
            yield Select([('All agent sessions','all')],value='all',allow_blank=False,id='scope')
            yield Static('',id='controls-spacer')
            yield Checkbox('Reviews',id='reviews')
            yield Button('Pause',id='pause')
        with TabbedContent(initial='control',id='pages'):
            with TabPane('0  Control',id='control'):
                with Vertical(id='control-pane'):
                    with Vertical(classes='box',id='control-launch'):
                        yield Static('Start an agent · your first prompt becomes its goal',id='control-launch-title')
                        yield TextArea(id='control-prompt',show_line_numbers=False,soft_wrap=True,
                                       placeholder='Describe the goal for this agent…')
                        with Horizontal(id='control-launch-row'):
                            yield Select([],prompt='Choose an installed agent',id='control-runtime')
                            yield Button('Start agent',id='control-start',variant='primary')
                            yield Static('Run agents, inspect progress, and answer requests here.',id='control-status',markup=False)
                    yield Static('',id='control-alert',markup=False)
                    with Horizontal(id='control-workspace'):
                        with Vertical(classes='box',id='control-sessions-box'):
                            yield ResultsTable(id='control-sessions',cursor_type='row',zebra_stripes=True)
                        with Vertical(classes='box',id='control-attention-box'):
                            yield ResultsTable(id='control-requests',cursor_type='row',zebra_stripes=True)
                            yield RichLog(id='control-detail',wrap=True,markup=False,auto_scroll=False)
                            yield TextArea(id='control-answer',show_line_numbers=False,soft_wrap=True)
                            with Horizontal(id='control-actions'):
                                yield Button('Approve once',id='control-approve-once',variant='success')
                                yield Button('Always allow',id='control-approve-always')
                                yield Button('Deny',id='control-deny',variant='error')
                                yield Button('Send answer',id='control-answer-send',variant='primary')
                                yield Button('AI summary',id='control-summary')
            with TabPane('1  Agents',id='agents'):
                yield self.results('agents','agent-table','Filter agents by task, project, model, or session…')
                with Horizontal(id='agent-bottom'):
                    with Vertical(id='agent-info'):
                        yield SelectableRichLog(id='agent-detail',classes='box',min_width=1,wrap=True,markup=False,auto_scroll=False)
                        with Horizontal(classes='inspector-actions'):
                            yield Button(Text('Session activity · s'),id='agent-open')
                            yield Button('AI summary',id='agent-summary',tooltip='Send this session’s task and up to 30 recent action headlines to your configured provider. Uses your API quota.')
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
                    yield self.results('mcp','mcp-table','Filter MCP calls by server, tool, arguments, or result…','list')
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
            if self.agent:
                with TabPane('6  Output',id='output'):
                    yield Static('stdout / stderr from the agent launched by this cockpit.',id='output-state',classes='hint')
                    yield SelectableRichLog(id='agent-output',classes='box',min_width=1,wrap=True,markup=False,auto_scroll=True)
        yield Static('Connecting to receiver…',id='health',markup=False)
        yield Footer()

    def get_default_screen(self):
        return ObservatoryScreen()

    def on_mount(self):
        columns={
            'control-sessions':[('Agent',11,0),('State',12,0),('Project',16,1),('Goal / task',30,3)],
            'control-requests':[('Needs input',18,1),('Session',12,1),('Request',10,0)],
            'agent-table':[('Agent',11,0),('Session ID',12,1),('Repo / project',14,2),('Status',12,0),('Updated',8,0),('Waiting on',16,2),('Task / details',22,4)],
            'agent-actions':[('Time',8,0),('Type',7,0),('Recent actions',15,1)],
            'activity-table':[('Time',8,0),('Type',7,0),('Result',9,0),('Action',20,1)],
            'mcp-servers':[('Observed server · Enter filters calls',30,3),('Calls',8,0),('Tools',8,0),('Errors',8,0)],
            'mcp-table':[('Time',8,0),('Agent',11,1),('Server',9,1),('Tool',14,3),('Result',7,0),('Latency',7,0)],
            'trace-table':[('Result',11,0),('Task / run',20,1),('Spans',5,0),('Tokens in / out',15,0)],
            'host-table':[('Process',10,1),('PID',7,0),('Project',12,2),('State',8,1),('RAM',9,0),('CPU total',9,0)]}
        for name,cols in columns.items():
            self.query_one('#'+name,ResultsTable).configure(cols)
        for name,title in [('results-agents','Agent sessions'),('agent-detail','Session overview'),('agent-actions','Recent work'),('results-activity','Activity stream'),('activity-inspector','Event detail'),('mcp-servers','Servers observed in this scope · all stored history'),('results-mcp','MCP calls'),('mcp-inspector','Call detail'),('results-traces','Traces'),('trace-detail','Execution tree'),('results-host','Agent processes'),('host-detail','Collection status')]:
            self.query_one('#'+name).border_title=title
        self.query_one('#control-sessions').border_title='All agent sessions'
        self.query_one('#control-requests').border_title='Needs input · requests you can handle here'
        self.query_one('#control-detail').border_title='Selected agent or request'
        self.query_one('#control-alert').display=False
        for name in ('control-answer','control-approve-once','control-approve-always','control-deny','control-answer-send','control-summary'):
            self.query_one('#'+name).display=False
        for page in self.search_queries:
            self.query_one('#results-'+page).border_subtitle='↑↓ select  ·  Enter inspect'
            self.query_one('#search-'+page).tooltip='Search this view · / to focus · Esc to return'
        self.query_one('#reviews').tooltip='Include internal approval review sessions'
        self.query_one('#pause').tooltip='Pause display updates · collection continues'
        for log in self.query(SelectableRichLog):
            log.tooltip='Drag to select text · Ctrl+C copies selection · c copies details'
        self.query_one('#host-detail').styles.height=8
        self.update_layout()
        self.screen.set_class(self.query_one('#pages',TabbedContent).active=='control','control-mode')
        if not self.feed.thread.is_alive():self.feed.thread.start()
        self.refresh_timer=self.set_interval(.3,self.refresh_data)
        self.control_timer=self.set_interval(1.5,self.refresh_control)
        self.control_alert_timer=self.set_interval(.65,self.flash_control_alert)
        self.refresh_control()
        self.query_one('#control-prompt',TextArea).focus()

    def on_unmount(self):
        if self.refresh_timer:self.refresh_timer.stop()
        if getattr(self,'control_timer',None):self.control_timer.stop()
        if getattr(self,'control_alert_timer',None):self.control_alert_timer.stop()
        for timer in self.search_timers.values():timer.stop()

    def refresh_control(self):
        if getattr(self,'control_poll_active',False):return
        self.control_poll_active=True
        self.control_request(lambda:self.feed.api.get('/api/control'),self.control_snapshot_received)

    def control_request(self,request,finished):
        def run():
            try:
                result=request();error=''
            except Exception as exc:
                result=None;error=str(exc)
            try:
                self.call_from_thread(finished,result,error)
            except RuntimeError:
                pass
        threading.Thread(target=run,name='observatory-control-request',daemon=True).start()

    def control_snapshot_received(self,snapshot,error):
        self.control_poll_active=False
        if error:
            self.control_failed(error)
            return
        self.apply_control_snapshot(snapshot)

    def control_failed(self,message):
        self.control_error=message
        if self.query('#control-status'):
            self.query_one('#control-status',Static).update('Control connection error: '+message)

    def flash_control_alert(self):
        alert=self.query('#control-alert')
        if alert and alert.first().display:
            self.control_alert_pulse=not getattr(self,'control_alert_pulse',False)
            alert.first().set_class(self.control_alert_pulse,'pulse')

    def apply_control_snapshot(self,snapshot):
        self.control_error=''
        self.control_snapshot=snapshot
        self.render_control()

    def render_control(self):
        if not self.query('#control-sessions'):
            return
        snapshot=self.control_snapshot
        available=snapshot.get('available_runtimes') or {}
        options=[(runtime,runtime) for runtime,enabled in available.items() if enabled]
        picker=self.query_one('#control-runtime',Select)
        current=picker.value
        runtime_options=tuple(options)
        if runtime_options!=self.control_runtime_options:
            picker.set_options(options or [('No installed agent CLI','_none_')])
            current=current if current in {value for _,value in options} else (options[0][1] if options else '_none_')
            picker.value=current
            self.control_runtime_options=runtime_options
        picker.disabled=not options
        sessions=snapshot.get('sessions') or []
        requests=snapshot.get('attention') or []
        alert=self.query_one('#control-alert',Static)
        if requests:
            request=requests[0]
            session=next((item for item in sessions if item.get('runtime')==request.get('runtime') and
                          item.get('session_id')==request.get('session_id')),{})
            project=session.get('project') or request.get('runtime') or 'Agent'
            alert.update(f"⚠ {project} needs input · {request.get('kind','request')}")
            alert.display=True
        else:
            alert.display=False
            alert.remove_class('pulse')
        self.query_one('#control-requests').border_title=f'Needs input · {len(requests)} pending'
        jobs=snapshot.get('launches') or []
        status_text='No compatible agent CLI found.' if not options else 'Run agents, inspect progress, and answer requests here.'
        if jobs:
            latest=jobs[0]
            status_text=f"{latest['runtime']} launch {latest['state']}"+((': '+latest['error']) if latest.get('error') else '')
        self.query_one('#control-status',Static).update(status_text)
        session_signature=tuple((self.control_session_key(item),item.get('state'),item.get('goal'),item.get('task'),
                                 item.get('updated_ns'),item.get('waiting_for')) for item in sessions)
        request_signature=tuple((item.get('id'),item.get('runtime'),item.get('session_id'),item.get('kind')) for item in requests)
        signature=(session_signature,request_signature)
        if signature!=self.control_signature:
            old_requests=set(self.control_tables['control-requests'])
            self.control_signature=signature
            session_table=self.query_one('#control-sessions',DataTable)
            session_table.clear(columns=False)
            self.control_tables['control-sessions']={}
            for item in sessions:
                key=self.control_session_key(item)
                self.control_tables['control-sessions'][key]=item
                session_table.add_row(cell(item.get('runtime') or 'Agent'),
                    cell(item.get('state') or 'unknown','#fbbf24' if item.get('state')=='waiting' else '#6ee7c2'),
                    cell(item.get('project') or '—'),cell(item.get('goal') or item.get('task') or 'Goal not captured'),key=key)
            request_table=self.query_one('#control-requests',DataTable)
            request_table.clear(columns=False)
            self.control_tables['control-requests']={}
            for item in requests:
                key=str(item.get('id') or '')
                if not key:continue
                self.control_tables['control-requests'][key]=item
                session=next((s for s in sessions if s.get('runtime')==item.get('runtime') and
                              s.get('session_id')==item.get('session_id')),{})
                request_table.add_row(cell(item.get('runtime') or 'Agent'),
                    cell(session.get('project') or str(item.get('session_id') or '')[-12:]),
                    cell(item.get('kind') or 'request','#fbbf24'),key=key)
            if self.selected_control_session not in self.control_tables['control-sessions']:
                self.selected_control_session=next(iter(self.control_tables['control-sessions']), '')
            if self.selected_control_request not in self.control_tables['control-requests']:
                self.selected_control_request=next(iter(self.control_tables['control-requests']), '')
            new_requests=set(self.control_tables['control-requests'])-old_requests
            if new_requests:
                request=self.control_tables['control-requests'][next(iter(new_requests))]
                self.notify(request.get('runtime','Agent')+' is asking for input.',severity='warning',timeout=10)
            self.render_control_detail()
        elif not self.selected_control_request and not self.selected_control_session and sessions:
            self.selected_control_session=self.control_session_key(sessions[0])
            self.render_control_detail()

    @staticmethod
    def control_session_key(session):
        return str(session.get('runtime') or '')+':'+str(session.get('session_id') or '')

    def load_control_session(self,runtime,session_id):
        self.control_request(lambda:self.feed.api.get('/api/control/session?'+urlencode({'runtime':runtime,'id':session_id})),
            lambda detail,error:self.control_session_received(runtime,session_id,detail,error))

    def control_session_received(self,runtime,session_id,detail,error):
        self.apply_control_session_detail(runtime,session_id,detail if not error else {'error':error})

    def apply_control_session_detail(self,runtime,session_id,detail):
        if self.selected_control_request:return
        if self.selected_control_session!=runtime+':'+session_id:return
        self.control_session_detail=detail
        self.render_control_detail()

    def render_control_detail(self):
        if not self.query('#control-detail'):return
        log=self.query_one('#control-detail',RichLog)
        log.clear()
        request=self.control_tables['control-requests'].get(self.selected_control_request)
        session=self.control_tables['control-sessions'].get(self.selected_control_session)
        for name in ('control-answer','control-approve-once','control-approve-always','control-deny','control-answer-send','control-summary'):
            self.query_one('#'+name).display=False
        if request:
            runtime=request.get('runtime','Agent');kind=request.get('kind','request')
            session_label=str(request.get('session_id') or '')
            log.write(Text(f'{runtime} · {kind} request',style='bold #e6edf3'))
            log.write(Text('\nSession '+session_label))
            details=request.get('detail') or {}
            if kind=='permission':
                log.write(Text('\nPermission: '+str(details.get('permission') or 'Approval requested'),style='bold #f2c779'))
                if details.get('metadata'):
                    log.write(Text('\n'+json.dumps(details['metadata'],ensure_ascii=False,indent=2)))
                self.query_one('#control-approve-once').display=True
                self.query_one('#control-deny').display=True
                if runtime=='OpenCode':self.query_one('#control-approve-always').display=True
            else:
                questions=details.get('questions') or []
                log.write(Text('\nAnswer each question below. For a multi-select question, separate choices with commas.'))
                for index,question in enumerate(questions,1):
                    log.write(Text(f"\n{index}. {question.get('question') or 'Question'}",style='bold #92b9eb'))
                    choices=[option.get('label','') for option in question.get('options') or []]
                    if choices:log.write(Text('  Choices: '+', '.join(choices)))
                    if question.get('description'):log.write(Text('  '+str(question['description'])))
                answer=self.query_one('#control-answer',TextArea)
                answer.text=''
                answer.placeholder='One answer per line'
                self.query_one('#control-answer').display=True
                self.query_one('#control-answer-send').display=True
            return
        if session:
            runtime=session.get('runtime','Agent');session_id=session.get('session_id','')
            detail=getattr(self,'control_session_detail',None)
            if not detail or detail.get('session_id')!=session_id:
                self.control_session_detail=None
                log.write(Text(f"{runtime} · {session.get('state','unknown')}",style='bold #e6edf3'))
                log.write(Text('\nLoading goal and recent activity…'))
                self.load_control_session(runtime,session_id)
            elif detail.get('error'):
                log.write(Text('Could not load session: '+detail['error'],style='#ff8b92'))
            else:
                log.write(Text(f"{runtime} · {session.get('state','unknown')}",style='bold #e6edf3'))
                log.write(Text('\nGoal: '+str(session.get('goal') or detail.get('goal') or 'Goal not captured')))
                if session.get('waiting_for'):log.write(Text('\nWaiting for: '+str(session['waiting_for']),style='#f2c779'))
                if session.get('state')=='waiting' and not session.get('can_reply'):
                    log.write(Text('\nThis existing session is observed only. Reply in its agent terminal.',style='#8b9bb0'))
                log.write(Text('\nRecent prompts and activity',style='bold #92b9eb'))
                events=detail.get('events') or []
                if not events:log.write(Text('\nNo recorded activity yet.'))
                for event in events[-8:]:
                    label=' · ERROR' if str(event.get('result') or '').lower() in {'error','failed'} else ''
                    log.write(Text(f"\n{event.get('domain','event')} / {event.get('action','update')}{label}"))
                    body=event.get('detail') or event.get('subject') or ''
                    if body:log.write(Text('\n  '+str(body)[:700]))
            self.query_one('#control-summary').display=True
            return
        log.write(Text('Select an agent or a pending request. Start an agent above to begin.',style='#8b9bb0'))

    def launch_control_agent(self):
        prompt=self.query_one('#control-prompt',TextArea).text.strip()
        runtime=self.query_one('#control-runtime',Select).value
        if not prompt:
            self.notify('Enter the first prompt. It becomes the agent goal.',severity='warning')
            self.query_one('#control-prompt').focus();return
        if runtime in {Select.BLANK,None,'_none_'}:
            self.notify('Choose an installed agent.',severity='warning');return
        self.query_one('#control-start',Button).disabled=True
        self.query_one('#control-status',Static).update('Starting '+str(runtime)+'…')
        self.post_control_launch(str(runtime),prompt)

    def post_control_launch(self,runtime,prompt):
        self.control_request(lambda:self.feed.api.post('/api/control/launch',{'runtime':runtime,'prompt':prompt}),
            lambda result,error:self.control_launch_finished(runtime,error))

    def control_launch_finished(self,runtime,error):
        self.query_one('#control-start',Button).disabled=False
        if error:
            self.query_one('#control-status',Static).update('Could not start agent: '+error)
        else:
            self.query_one('#control-prompt',TextArea).text=''
            self.query_one('#control-status',Static).update(runtime+' started. Watching for progress and requests.')
            self.refresh_control()

    def send_control_reply(self,decision=None,answer=False):
        request=self.control_tables['control-requests'].get(self.selected_control_request)
        if not request:return
        payload={'runtime':request['runtime'],'session_id':request['session_id'],'kind':request['kind'],'request_id':request['id']}
        if request['kind']=='permission':payload['decision']=decision
        else:
            questions=(request.get('detail') or {}).get('questions') or []
            lines=self.query_one('#control-answer',TextArea).text.splitlines()
            if len(lines)!=len(questions) or any(not line.strip() for line in lines):
                self.notify('Enter one answer on each line.',severity='warning');return
            payload['answers']=[([choice.strip() for choice in line.split(',') if choice.strip()]
                if question.get('multiple') or question.get('multiSelect') else [line.strip()])
                for line,question in zip(lines,questions)]
        self.query_one('#control-status',Static).update('Sending response…')
        self.post_control_reply(payload,request['runtime'])

    def post_control_reply(self,payload,runtime):
        self.control_request(lambda:self.feed.api.post('/api/control/reply',payload),
            lambda result,error:self.control_reply_finished(runtime,error))

    def control_reply_finished(self,runtime,error):
        if error:
            self.notify('Could not send response: '+error,severity='error',timeout=8)
            return
        self.query_one('#control-status',Static).update('Response sent to '+runtime+'.')
        self.selected_control_request=''
        self.refresh_control()

    def summarize_control_session(self):
        session=self.control_tables['control-sessions'].get(self.selected_control_session)
        if not session:return
        self.query_one('#control-summary',Button).disabled=True
        self.post_control_summary(session['runtime'],session['session_id'])

    def post_control_summary(self,runtime,session_id):
        self.control_request(lambda:self.feed.api.post('/api/control/summary',{'runtime':runtime,'session_id':session_id}),
            lambda result,error:self.control_summary_finished(result.get('summary','No summary returned.') if result else '',error))

    def control_summary_finished(self,summary,error):
        self.query_one('#control-summary',Button).disabled=False
        if error:
            self.notify('AI summary unavailable: '+error,severity='warning',timeout=10)
            return
        log=self.query_one('#control-detail',RichLog);log.clear()
        log.write(Text('AI progress summary',style='bold #6ee7c2'))
        log.write(Text('\n'+summary))
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
        old_index=table.cursor_row
        with table.prevent(DataTable.RowHighlighted):
            if [key.value for key in table.rows]==[key for key,_,_ in rows]:
                for key,values,_ in rows:
                    for column,value in zip(table.columns,values):table.update_cell(key,column,value)
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
        if self.screen.selections:
            self.query_one('#health',Static).update('Text selected · Ctrl+C to copy · click to clear selection and resume display updates')
            return
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
        self.stat('sessions',len(work),'Agent sessions',f'{recent} updated recently · {sum(item["state"]=="Waiting" for item in work)} waiting')
        self.stat('processes',live,'Host processes','PIDs on this host')
        servers={item.get('server') for item in data.mcp if item.get('server') and item.get('server')!='Not reported'}
        operations={(item.get('server'),item.get('tool')) for item in data.mcp if item.get('tool') and item.get('tool')!='Not reported'}
        mcp_errors=sum(str(item.get('status','')).upper() in {'ERROR','FAILED'} for item in data.mcp)
        inventory=data.mcp_summary
        call_count=inventory.get('calls',len(data.mcp))
        server_count=len(inventory['servers']) if inventory else len(servers)
        tool_count=inventory.get('tools',len(operations))
        mcp_errors=inventory.get('errors',mcp_errors)
        self.stat('mcp-count',quantity(call_count,'call')+('' if self.size.width<110 else ' · '+quantity(server_count,'server')),'Observed MCP',
                  f'{quantity(server_count,"server")} · {quantity(mcp_errors,"error")}' if self.size.width<110 else f'{quantity(tool_count,"tool")} · {quantity(mcp_errors,"error")}')
        self.query_one('#stat-mcp-count').tooltip='All stored calls in the selected scope. Configured servers appear only after an observed call.\n'+', '.join(row['server'] for row in inventory.get('servers',[]))
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
                        (not agent_query or agent_query in ' '.join(str(item.get(key) or '') for key in ('runtime','service','project','working_directory','branch','task','model','session_id','id','state','waiting_for')).casefold())]
        self.fill('agent-table',[(item['id'],[
            cell(item['runtime']),cell(item.get('session_id') or item['id'],'#a4b7cb'),
            cell(item.get('project') or '—'),
            cell(item['state'],'#fbbf24' if item['state']=='Waiting' else '#6ee7c2' if item['recent'] else '#8b9bb0'),
            cell(age(item['stamp']),'#8b9bb0'),cell(item.get('waiting_for') or '—'),
            cell(item.get('task') or 'Task not reported')],item) for item in visible_agents])
        self.search_status('agents',len(visible_agents),'session')
        if self.selected_source not in {item['id'] for item in visible_agents}:self.selected_source=visible_agents[0]['id'] if visible_agents else None
        self.show_agent()
        activity_items=self.visible_results('activity',data.activity_results if data.activity_results is not None else data.activity)
        activity_items=self.hold_selected('activity',activity_items,self.selected_activity,'source_id')
        activity=[(item.get('id') or item['source_id'],[cell(table_when(item['time_unix_nano'])),cell(item['domain'].upper(),'#92b9eb'),status(item.get('result')),cell(headline(item))],item) for item in activity_items]
        self.fill('activity-table',activity)
        self.search_status('activity',len(activity),'event')
        if self.selected_activity not in self.table_data.get('activity-table',{}):self.selected_activity=activity[0][0] if activity else None
        self.show_activity()
        self.fill('mcp-servers',[(row['server'],[cell(row['server']),cell(number(row['calls'])),cell(number(row['tools'])),cell(number(row['errors']))],row) for row in data.mcp_summary.get('servers',[])])
        mcp_items=self.visible_results('mcp',data.mcp_results if data.mcp_results is not None else data.mcp)
        mcp_items=self.hold_selected('mcp',mcp_items,self.selected_mcp,'id')
        self.fill('mcp-table',[(item['id'],[cell(table_when(item['time_unix_nano']),'#8b9bb0'),cell((item.get('service') or 'Unknown')+' · '+str(item.get('session_id') or '—')[-6:]),cell(item['server'],'#a4b7cb'),cell(item['tool']),status(item['status']),cell('—' if item['duration_ms'] is None else timing(item['duration_ms']))],item) for item in mcp_items])
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
        else:
            mcp_hint='No MCP operations in this scope · General tool activity is available in Activity'
        if 'mcp' in self.errors_only:mcp_hint=f"{quantity(len(mcp_items),'failed call')} · errors only · current results"
        self.query_one('#mcp-hint',Static).update(mcp_hint)
        traces=self.visible_results('traces',data.traces[:])
        detail=data.detail
        if self.feed.pinned_trace and detail.get('summary') and not any(item['trace_id']==detail['trace_id'] for item in traces):traces.insert(0,detail['summary'])
        self.fill('trace-table',[(item['trace_id'],[cell('Active · errors','yellow') if item.get('active') and item['status']=='ERROR' else cell('Error','red') if item['status']=='ERROR' else status(item['status']),cell(trace_name(item)),cell(item['span_count']),cell(number(item['input_tokens'])+' / '+number(item['output_tokens']))],item) for item in traces],preserve=self.feed.selected_trace)
        self.search_status('traces',len(traces),'trace')
        for page in ('activity','mcp','traces'):
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
        self.inspect('host-detail','Host & collection status',[
            ('Host',runtime.get('host','Not reported')),('Platform',str(runtime.get('os',''))+' / '+str(runtime.get('architecture',''))),
            ('Load average',', '.join(str(value) for value in runtime.get('load_average',[])) or 'Unavailable'),
            ('Sources',f"{data.local_status.get('sources',0)} recent files · lookback {data.local_status.get('lookback_hours',24)}h · read errors {data.local_status.get('read_errors',0)}"),
            ('Coverage','Source logs can be partial. Costs and model-call latency are unavailable unless reported. CPU is cumulative process time.')],[])

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
        if isinstance(value,str) and label.casefold() in {'recorded content','text','output','response'}:
            segments=embedded_json(value)
            if any(kind=='json' for kind,_ in segments):
                log.write(Text('\n'+label.title(),style='bold #92b9eb'))
                for kind,content in segments:
                    if kind=='text':
                        if content.strip():log.write(Text(content.strip('\n')))
                    else:
                        write_json(log,content)
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
        self.query_one('#agent-summary',Button).disabled=not bool(item) or self.summary_busy
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
        generated=self.ai_summaries.get(item['id'])
        ai_sections=[]
        if generated:
            stamp,text=generated
            label='AI summary · earlier snapshot' if stamp!=item.get('stamp') else 'AI summary · verify against recorded events'
            ai_sections=[(label,text)]
        self.inspect('agent-detail',clean(item.get('task') or 'Task not reported'),[
            ('Agent',item['runtime']),('Session ID',item.get('session_id') or item['id']),
            ('Repo / project',item.get('project') or 'Not reported'),('Branch',item.get('branch') or 'Not reported'),
            ('Status',item['state']),('Waiting on',item.get('waiting_for') or 'No wait reported'),
            ('Last updated',when(item['stamp'])+' · '+age(item['stamp']) if item['stamp'] else 'Not reported'),
            ('Latest',headline(latest) if latest else 'Not reported'),
            ('Model',item.get('model') or 'Not reported'),
            ('Work',f"{summary.get('trace_count',0)} traces · {item.get('spans',0)} spans · {number(summary.get('input_tokens'))} in / {number(summary.get('output_tokens'))} out"),
            ('Cost','Not reported' if not summary or summary.get('unknown_cost_runs') else f"${summary.get('known_cost_usd',0):.4f} known")],
            ai_sections+[('Working directory',item.get('working_directory')),
             ('Status evidence','No new session records for 35 seconds; the last reported wait may no longer be current.' if item['state']=='No updates' else 'Status comes from recorded lifecycle events. A tool response wait is not an approval request.')])
        actions=[event for event in self.data.activity if event.get('session_id')==item.get('session_id')] if item.get('session_id') else [event for event in self.data.activity if event.get('service')==item.get('service')]
        meaningful=[event for event in actions if event.get('domain') in {'shell','file','mcp','browser','model','message'}]
        actions=sorted(meaningful or actions,key=lambda event:event.get('time_unix_nano',0),reverse=True)
        self.fill('agent-actions',[(event.get('id') or event['source_id'],[cell(table_when(event['time_unix_nano'])),cell(event['domain'].upper()),cell(headline(event))],event) for event in actions[:30]])

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
        if event.data_table.id in self.control_tables:
            item=self.control_tables[event.data_table.id].get(event.row_key.value)
            if item:
                if event.data_table.id=='control-sessions':
                    self.selected_control_session=self.control_session_key(item)
                    self.selected_control_request=''
                else:
                    self.selected_control_request=str(item.get('id') or '')
                self.render_control_detail()
            return
        item=self.valid_row(event)
        if not item:return
        name=event.data_table.id
        if name=='agent-table':self.selected_source=item['id'];self.show_agent()
        elif name=='activity-table':self.selected_activity=event.row_key.value;self.show_activity()
        elif name=='mcp-table':self.selected_mcp=item['id'];self.show_mcp()
        elif name=='trace-table':
            if self.feed.pinned_trace and self.data.detail.get('trace_id')!=self.feed.selected_trace:return
            if self.feed.selected_trace!=item['trace_id']:
                self.feed.selected_trace=item['trace_id'];self.feed.pinned_trace=True;self.feed.request_refresh()
                self.feed.selected_span_id=None
                self.query_one('#span-tree',Tree).root.set_label(Text('Loading selected trace…'))

    @on(DataTable.RowSelected)
    def row_selected(self,event):
        if event.data_table.id in self.control_tables:
            self.row_highlighted(event)
            return
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
        self.screen.set_class(event.pane.id=='control','control-mode')

    @on(Button.Pressed)
    def button_pressed(self,event):
        if event.button.id=='control-start':self.launch_control_agent()
        elif event.button.id=='control-approve-once':
            request=self.control_tables['control-requests'].get(self.selected_control_request)
            if request:self.send_control_reply('accept' if request.get('runtime')=='Codex' else 'once')
        elif event.button.id=='control-approve-always':self.send_control_reply('always')
        elif event.button.id=='control-deny':
            request=self.control_tables['control-requests'].get(self.selected_control_request)
            if request:self.send_control_reply('decline' if request.get('runtime')=='Codex' else 'reject')
        elif event.button.id=='control-answer-send':self.send_control_reply(answer=True)
        elif event.button.id=='control-summary':self.summarize_control_session()
        elif event.button.id=='pause':self.action_pause()
        elif event.button.id=='history-back':self.action_history_back()
        elif event.button.id.startswith(('older-','newer-','latest-')):
            direction,page=event.button.id.split('-',1)
            self.action_history(direction,page)
        elif event.button.id.startswith('expand-'):self.action_expand()
        elif event.button.id.startswith('errors-'):
            page=event.button.id.removeprefix('errors-')
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
        elif event.button.id in {'activity-trace','mcp-trace'}:self.action_open_trace()
        elif event.button.id in {'activity-copy','mcp-copy'}:self.action_copy()

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
        self.summarize_agent(dict(item),config)

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
        old=self.selected_activity,self.selected_mcp
        if page=='activity':self.selected_activity=None
        if page=='mcp':self.selected_mcp=None
        if not self.feed.navigate_history(page,direction):
            self.selected_activity,self.selected_mcp=old
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
        else:
            name={'agents':'agent-detail','traces':'span-info','host':'host-detail','output':'agent-output'}[page]
            self.copy_to_clipboard(self.query_one('#'+name,SelectableRichLog).plain_text)
            return
        if not item:
            self.notify('Select an activity or MCP call to copy.')
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
        target={'agents':'agent-detail','activity':'activity-detail','mcp':'mcp-detail','traces':'span-tree','host':'host-detail'}[page]
        self.query_one('#'+target).focus()
        self.notify('Expanded detail · Esc to return',timeout=2)
    def action_focus_agent(self):
        if self.selected_source:
            self.query_one('#scope',Select).value=self.selected_source
            self.action_page('activity')
    def action_reviews(self):self.query_one('#reviews',Checkbox).toggle()
    def action_all(self):
        self.selected_activity=self.selected_mcp=None
        self.query_one('#scope',Select).value='all'
        self.errors_only.clear()
        for button in self.query('.error-filter'):
            button.remove_class('-on');button.label='Errors'
        for page in self.search_queries:self.query_one('#search-'+page,Input).value=''
        self.last_update=-1;self.refresh_data()
    def action_page(self,page):
        if page=='output' and not self.agent:return
        self.screen.clear_selection()
        self.collapse(page)
        self.query_one('#pages',TabbedContent).active=page
        if page=='control':
            self.query_one('#control-prompt',TextArea).focus()
            return
        target={'agents':'agent-table','activity':'activity-table','mcp':'mcp-table','traces':'trace-table','host':'host-table','output':'agent-output'}[page]
        self.query_one('#'+target).focus()
    def action_browser(self):webbrowser.open(self.feed.api.endpoint)
    def action_open_trace(self):
        page=self.query_one('#pages',TabbedContent).active
        if page not in {'activity','mcp'}:return
        item=self.table_data.get('mcp-table',{}).get(self.selected_mcp) if page=='mcp' else self.table_data.get('activity-table',{}).get(self.selected_activity)
        if not item or not item.get('trace_id'):
            self.notify('This action has no linked trace.');return
        self.remember_location()
        self.jump_span=item.get('span_id')
        self.feed.selected_span_id=self.jump_span
        self.feed.selected_trace=item['trace_id'];self.feed.pinned_trace=True;self.feed.request_refresh();self.action_page('traces')
