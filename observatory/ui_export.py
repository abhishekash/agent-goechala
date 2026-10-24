"""Explicit, previewed exports of selected evidence, never receiver configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RichLog, Static

FIELDS = {
    'agents': ('runtime', 'service', 'session_id', 'project', 'model', 'task', 'state', 'waiting_for'),
    'activity': ('id', 'service', 'session_id', 'trace_id', 'span_id', 'time_unix_nano', 'domain', 'action', 'subject', 'result', 'detail', 'duration_ms'),
    'mcp': ('id', 'service', 'session_id', 'trace_id', 'span_id', 'time_unix_nano', 'server', 'tool', 'method', 'status', 'arguments', 'output', 'duration_ms'),
    'traces': ('trace_id', 'summary', 'spans', 'logs', 'span_page', 'pagination',
               'evidence_window', 'findings', 'progress', 'outcome'),
    'metrics': ('service', 'name', 'kind', 'temporality', 'scope_name', 'unit', 'description', 'point_count'),
    'findings': ('id', 'severity', 'kind', 'title', 'detail', 'task', 'session_id', 'trace_id', 'services', 'evidence', 'occurrence_count', 'time_unix_nano'),
}


def investigation_json(view, record):
    """Freeze the exact selected payload before showing the confirmation."""
    evidence = {key: record[key] for key in FIELDS.get(view, ()) if key in record}
    return json.dumps({'format': 'agenturm-investigation-v1', 'view': view,
                       'coverage': 'Selected record and currently loaded evidence only; not a complete history export.',
                       'evidence': evidence}, ensure_ascii=False, indent=2) + '\n'


def write_investigation(path, content):
    """Create owner-only files; refuse to overwrite files or follow symlinks."""
    target = Path(path).expanduser()
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
    except BaseException:
        # Do not leave a truncated export that could be mistaken for a complete one.
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            target.unlink()
        except OSError:
            pass
        raise


class ExportScreen(ModalScreen):
    DEFAULT_CSS = '''
    ExportScreen { align: center middle; background: $background 70%; }
    #export-dialog { width: 90%; height: 90%; border: round $warning; padding: 1 2; background: $surface; }
    #export-warning { height: auto; margin-bottom: 1; }
    #export-preview { height: 1fr; }
    #export-path { height: 3; }
    #export-actions { height: 3; }
    #export-actions Button { width: 1fr; }
    '''
    BINDINGS = [('escape', 'dismiss', 'Cancel')]

    def __init__(self, view, record):
        super().__init__()
        self.content = investigation_json(view, record)

    def compose(self) -> ComposeResult:
        with Vertical(id='export-dialog'):
            yield Static('PRIVACY PREVIEW · Nothing has been written.\nThis exact JSON may contain prompts, outputs, paths, session/trace IDs, or secrets recorded by your agent. It is NOT redacted. Review before sharing. Only the selected record is included; no receiver credentials or background history are added.', id='export-warning', markup=False)
            yield RichLog(id='export-preview', wrap=True, markup=False)
            yield Input(placeholder='Destination file (must not already exist)', id='export-path')
            with Horizontal(id='export-actions'):
                yield Button('Cancel', id='export-cancel')
                yield Button('Write reviewed export', id='export-confirm', variant='warning')

    def on_mount(self):
        self.query_one(RichLog).write(Text(self.content))
        self.query_one('#export-cancel', Button).focus()

    @on(Button.Pressed, '#export-cancel')
    def cancel(self):
        self.dismiss()

    @on(Button.Pressed, '#export-confirm')
    def confirm(self):
        path = self.query_one('#export-path', Input).value.strip()
        if not path:
            self.notify('Choose a destination file first.', severity='warning')
            return
        try:
            write_investigation(path, self.content)
        except FileExistsError:
            self.notify('Destination already exists. Choose a new file; nothing was overwritten.', severity='warning')
            return
        except (OSError, ValueError):
            self.notify('Could not write export. Check the destination and permissions.', severity='error')
            return
        self.dismiss()
        self.app.notify('Selected investigation exported. Review again before sharing.')
