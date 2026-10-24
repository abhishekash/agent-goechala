"""Read-only local collection diagnostics and retention guidance."""
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RichLog

from .terminal import clean

RETENTION = '''RETENTION · logical deletion, not secure erasure
On the receiver host, choose your database and retention period explicitly:
  agenturm --db /path/to/observatory.db prune --older-than-days 7
For ongoing cleanup, start the receiver with serve --retention-days 7.
No retention policy is assumed here; the receiver does not report its policy.
Deleted content can remain in SQLite free pages and backups. Native source
logs are separate and are not deleted by pruning telemetry.'''

FIRST_RUN = '''FIND YOUR FIRST SESSION
Start or continue Codex, Claude Code, or OpenCode in its own terminal, then
return here. Discovery normally looks back 24 hours; Reviews are hidden.
If no session appears, check Diagnostics for read errors and source coverage.
For SDK/OTLP agents, point the producer at your local receiver using the
integration guide in docs/receiver.md. Agenturm observes; it does not run agents.'''


def diagnostics_text(local):
    lines = ['LOCAL COLLECTION DIAGNOSTICS',
             'Local paths may identify projects/users. No endpoint credentials or raw events are shown.',
             f"Discovery: {'enabled' if local.get('enabled') else 'disabled or not reported'} · lookback {local.get('lookback_hours', 'not reported')}h",
             f"Read errors: {local.get('read_errors', 0)} · OpenCode read errors: {local.get('opencode_read_errors', 0)}",
             f"Omitted older sources: {local.get('omitted_sources', 0)} · OpenCode: {local.get('opencode_omitted_sessions', 0)}"]
    diagnostics = [row.get('diagnostics') or {} for row in local.get('sessions', [])]
    if isinstance(local.get('opencode_diagnostics'), dict):
        diagnostics.append(local['opencode_diagnostics'])
    seen = set()
    for record in diagnostics:
        path = record.get('source_path')
        if not path or path in seen:
            continue
        seen.add(path)
        lines.extend(['', str(path),
                      f"Last successful read (Unix ns): {record.get('last_success_ns') or 'not reported'}",
                      f"Backlog bytes: {record.get('backlog_bytes', 'not reported')} · skipped oversized: {record.get('skipped_oversized_records', 'not reported')}",
                      f"Schema errors: {record.get('schema_errors', 'not reported')} · skipped records: {record.get('skipped_records', 'not reported')}"])
    if not seen:
        lines.extend(['', 'Per-source read diagnostics not reported by this receiver.'])
    lines.extend(['', RETENTION, '', FIRST_RUN])
    # Literal text, not terminal escape sequences or Rich markup from source paths.
    return '\n'.join(clean(line) for line in '\n'.join(lines).splitlines())


class DiagnosticsScreen(ModalScreen):
    DEFAULT_CSS = '''
    DiagnosticsScreen { align: center middle; background: $background 70%; }
    #diagnostics-dialog { width: 90%; height: 90%; border: round $primary; background: $surface; padding: 1 2; }
    #diagnostics-log { height: 1fr; }
    #diagnostics-close { width: 100%; }
    '''
    BINDINGS = [('escape', 'dismiss', 'Close')]

    def __init__(self, local):
        super().__init__()
        self.local = local

    def compose(self) -> ComposeResult:
        with Vertical(id='diagnostics-dialog'):
            yield RichLog(id='diagnostics-log', wrap=True, markup=False)
            yield Button('Close · Esc', id='diagnostics-close')

    def on_mount(self):
        self.query_one(RichLog).write(Text(diagnostics_text(self.local)))

    @on(Button.Pressed, '#diagnostics-close')
    def close_dialog(self):
        self.dismiss()
