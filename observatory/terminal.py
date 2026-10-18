"""Receiver lifecycle and live data for the terminal cockpit."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import textwrap
import unicodedata
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit


class CockpitError(Exception):
    pass


def clean(value):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    return "".join(char for char in text if unicodedata.category(char) not in {"Cc", "Cf"})


def clipped(value, width):
    result, used = [], 0
    for char in clean(value):
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + size > max(0, width):
            break
        result.append(char)
        used += size
    return "".join(result)


def number(value):
    value = float(value or 0)
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if abs(value) >= 10_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:,.0f}"


def trace_name(trace):
    if trace.get("task"):
        return trace["task"]
    if trace.get("models"):
        return ", ".join(trace["models"])
    return trace.get("name") or trace.get("trace_id", "Agent run")[:12]


class Api:
    def __init__(self, endpoint, token=None):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise CockpitError("Use a receiver HTTP address for --endpoint.")
        self.endpoint = endpoint.rstrip("/")
        self.token = token

    def get(self, path):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        request = urllib.request.Request(self.endpoint + path, headers=headers)
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)


class LocalReceiver:
    def __init__(self, db_path, port, token, local_agents=True, metadata_only=False, opencode_url=None):
        from http.server import ThreadingHTTPServer
        from .core import Store
        from .server import handler_for
        self.store = Store(db_path, async_derived_refresh=True)
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(
                self.store, token, quiet=True, endpoint=f"http://127.0.0.1:{port}", opencode_url=opencode_url))
        except OSError:
            self.store.close()
            raise
        self.thread = threading.Thread(target=self.server.serve_forever, name="observatory-receiver", daemon=True)
        self.thread.start()
        self.local = None
        if local_agents:
            from .local_agents import LocalAgents
            self.local = LocalAgents(self.store, capture_details=not metadata_only).start()
        self.store.local_agent_monitor = self.local

    def close(self):
        if self.local:
            self.local.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.close()


class AgentProcess:
    def __init__(self, target, arguments, endpoint, service, python, module=False, capture_content=False):
        command = [python, "-u", "-m", "observatory", "run", "--endpoint", endpoint, "--service", service]
        if module:
            command.append("--module")
        if capture_content:
            command.append("--capture-content")
        command.extend([target, *arguments])
        self.lines = deque(maxlen=400)
        self.lines.append("Starting " + clean(target))
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace",
                                        start_new_session=os.name != "nt")
        self.thread = threading.Thread(target=self._read, name="observatory-agent-output", daemon=True)
        self.thread.start()

    def _read(self):
        with self.process.stdout:
            for line in self.process.stdout:
                self.lines.append(clean(line.rstrip()))

    def close(self):
        if self.process.poll() is None:
            if os.name != "nt":
                try:
                    os.killpg(self.process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    self.process.kill()
                self.process.wait()
        self.thread.join(timeout=1)


def start_local_opencode():
    """Start a loopback OpenCode server for one-command local use."""
    if os.environ.get("OBSERVATORY_OPENCODE_URL"):
        return None, None
    executable = shutil.which("opencode")
    if not executable:
        return None, None
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
    except OSError:
        return None, None
    password = os.environ.get("OBSERVATORY_OPENCODE_PASSWORD") or os.environ.get("OPENCODE_SERVER_PASSWORD")
    env = os.environ.copy()
    if password:
        env["OPENCODE_SERVER_PASSWORD"] = password
    try:
        process = subprocess.Popen([executable, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=env,
                                   start_new_session=os.name != "nt")
    except OSError:
        return None, None
    from .control import ControlError, OpenCode
    url = f"http://127.0.0.1:{port}"
    client = OpenCode(url=url, password=password,
                      username=os.environ.get("OBSERVATORY_OPENCODE_USERNAME", "opencode"))
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            process.wait()
            return None, None
        try:
            client.request("GET", "/session")
            return process, url
        except ControlError:
            time.sleep(.1)
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
    return None, None


@dataclass
class Snapshot:
    totals: dict = field(default_factory=dict)
    agents: list = field(default_factory=list)
    traces: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    error: str = ""
    updated: float = 0
    local_status: dict = field(default_factory=dict)
    activity: list = field(default_factory=list)
    sessions: list = field(default_factory=list)
    mcp: list = field(default_factory=list)
    mcp_summary: dict = field(default_factory=dict)
    mcp_available: bool = True
    activity_results: list | None = None
    mcp_results: list | None = None
    queries: dict = field(default_factory=dict)
    pagination: dict = field(default_factory=dict)


class Feed:
    def __init__(self, api):
        self.api = api
        self.snapshot = Snapshot()
        self.service = ""
        self.query = ""
        self.activity_query = ""
        self.mcp_query = ""
        self.errors_only = False
        self.selected_trace = None
        self.selected_span_id = None
        self.session_id = ''
        self.include_reviews = False
        self.pinned_trace = False
        self.extra_cache = None
        self.extra_updated = 0
        self.extra_scope = None
        self.paused = False
        self.history = {page: [None] for page in ('activity','mcp','traces')}
        self.history_scopes = {}
        self.refresh_once = False
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="observatory-terminal-feed", daemon=True)

    def refresh(self):
        service, query, errors_only, selected = self.service, self.query, self.errors_only, self.selected_trace
        activity_query, mcp_query = self.activity_query, self.mcp_query
        requested_trace, requested_span = selected, self.selected_span_id
        session_id, include_reviews = self.session_id, self.include_reviews
        for page, text in (('activity',activity_query),('mcp',mcp_query),('traces',query)):
            scope=(service,session_id,text,include_reviews)
            if self.history_scopes.get(page)!=scope:
                self.history[page]=[None]
                self.history_scopes[page]=scope
        history={page:tuple(cursors) for page,cursors in self.history.items()}
        pagination={}
        def page_result(page, response):
            pagination[page]={'page':len(history[page]), 'next_cursor':response.get('next_cursor'),
                              'supported':'next_cursor' in response}
        try:
            local_status = self.api.get("/api/health").get("local_agents", {})
            totals = self.api.get("/api/metrics")
            agents = self.api.get("/api/connections")["connections"]
            try:
                activity_params = {"limit": 200, "service": service, "session_id": session_id,
                                   "include_reviews":int(include_reviews)}
                activity_page = self.api.get("/api/activity?" + urlencode(activity_params))
                activity = activity_page['activity']
                if activity_query or history['activity'][-1]:
                    activity_page = self.api.get("/api/activity?" + urlencode({**activity_params,
                        "q":activity_query,"cursor":history['activity'][-1] or ''}))
                activity_results = activity_page['activity']
                page_result('activity',activity_page)
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                activity, activity_results = [], []
            scope = (service, session_id, mcp_query, include_reviews, history['mcp'])
            if self.extra_cache is None or scope != self.extra_scope or time.monotonic()-self.extra_updated>3:
                sessions = self.api.get('/api/sessions?limit=500')['sessions']
                try:
                    mcp_params = {'limit':100,'service':service,'session_id':session_id,
                                  'include_reviews':int(include_reviews)}
                    mcp_page = self.api.get('/api/mcp?'+urlencode(mcp_params))
                    mcp = mcp_page['calls']
                    mcp_summary = mcp_page.get('summary', {})
                    if mcp_query or history['mcp'][-1]:
                        mcp_page = self.api.get('/api/mcp?'+urlencode({**mcp_params,'q':mcp_query,
                            'cursor':history['mcp'][-1] or ''}))
                    mcp_results = mcp_page['calls']
                    mcp_available = True
                except urllib.error.HTTPError as exc:
                    if exc.code!=404:
                        raise
                    mcp,mcp_results,mcp_available,mcp_page,mcp_summary=[],[],False,{},{}
                self.extra_cache = sessions,mcp,mcp_results,mcp_available,mcp_page,mcp_summary
                self.extra_scope,self.extra_updated = scope,time.monotonic()
            sessions,mcp,mcp_results,mcp_available,mcp_page,mcp_summary=self.extra_cache
            page_result('mcp',mcp_page)
            params = {"limit": 100, "service": service, "session_id": session_id, "q": query,
                      "cursor":history['traces'][-1] or '', 'include_reviews':int(include_reviews)}
            if errors_only:
                params["status"] = "ERROR"
            trace_page = self.api.get("/api/traces?" + urlencode(params))
            traces = trace_page['traces']
            page_result('traces',trace_page)
            if not include_reviews:
                traces = [item for item in traces if 'codex-review' not in item.get('services',[])]
                activity = [item for item in activity if item.get('service')!='codex-review']
                activity_results = [item for item in activity_results if item.get('service')!='codex-review']
                mcp = [item for item in mcp if item.get('service')!='codex-review']
                mcp_results = [item for item in mcp_results if item.get('service')!='codex-review']
            traces,activity = traces[:100],activity[:200]
            if not self.pinned_trace and selected not in {item["trace_id"] for item in traces}:
                selected = traces[0]["trace_id"] if traces else None
            detail = self.api.get("/api/traces/" + selected + "?span_limit=100") if selected else {}
            if selected and requested_span:
                wanted=requested_span
                known={span['span_id'] for span in detail.get('spans',[])}
                for _ in range(8):
                    if not wanted or wanted in known:break
                    page=self.api.get('/api/traces/'+selected+'/spans?'+urlencode({'limit':100,'q':wanted}))
                    span=next((item for item in page['spans'] if item['span_id']==wanted),None)
                    if not span:break
                    detail['spans'].append(span);known.add(wanted)
                    wanted=span.get('parent_span_id')
            findings = detail.get("findings", [])
            if not selected:
                findings = self.api.get("/api/findings?limit=8")["findings"]
            # A filter changed while the HTTP reads were in flight; discard the older view.
            if (service, query, activity_query, mcp_query, errors_only, session_id, include_reviews) != (self.service, self.query, self.activity_query, self.mcp_query, self.errors_only, self.session_id, self.include_reviews):
                self.wake.set()
                return
            if self.selected_trace!=requested_trace or self.selected_span_id!=requested_span or history!={page:tuple(cursors) for page,cursors in self.history.items()}:
                self.wake.set()
                return
            self.selected_trace = selected
            self.snapshot = Snapshot(totals, agents, traces, detail, findings, updated=time.time(), local_status=local_status, activity=activity,
                                     sessions=sessions,mcp=mcp,mcp_available=mcp_available,mcp_summary=mcp_summary,
                                     activity_results=activity_results[:200],mcp_results=mcp_results,
                                     queries={'activity':activity_query,'mcp':mcp_query,'traces':query},pagination=pagination)
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 401:
                message = "Receiver requires authentication. Set OBSERVATORY_TOKEN and restart."
            else:
                message = "Receiver unavailable. Retrying: " + clean(str(exc))
            old = self.snapshot
            from dataclasses import replace
            self.snapshot = replace(old,error=message)

    def request_refresh(self):
        self.refresh_once = True
        self.wake.set()

    def navigate_history(self, page, direction):
        cursors=self.history[page]
        next_cursor=self.snapshot.pagination.get(page,{}).get('next_cursor')
        if direction=='older' and next_cursor:
            self.history[page]=[*cursors,next_cursor]
        elif direction=='newer' and len(cursors)>1:
            self.history[page]=cursors[:-1]
        elif direction=='latest':
            self.history[page]=[None]
        else:
            return False
        if page=='traces':
            self.selected_trace=None;self.selected_span_id=None;self.pinned_trace=False
        self.request_refresh()
        return True

    def _loop(self):
        while not self.stop.is_set():
            if not self.paused or self.refresh_once:
                self.refresh_once = False
                self.refresh()
            self.wake.wait(1)
            self.wake.clear()

    def close(self):
        self.stop.set()
        self.wake.set()
        self.thread.join(timeout=4)


def agent_rows(snapshot):
    rows = [("", "ALL AGENTS", "")]
    known = set()
    local_groups = {}
    for agent in snapshot.agents:
        service = agent["service"]
        known.add(service)
        local = "local-session-log" in agent.get("integrations", [])
        if local:
            local_groups.setdefault(service, []).append(agent)
            continue
        state = ("RECENT" if agent["state"] == "running" else "IDLE") if local else (
            "LIVE" if agent["state"] == "running" else "DONE" if agent["state"] == "stopped" else "OFFLINE")
        rows.append((service, service, f"{state} · {number(agent['span_count'])} spans"))
    for service, agents in local_groups.items():
        state = "RECENT" if any(agent["state"] == "running" for agent in agents) else "IDLE"
        rows.append((service, service, f"{state} · {len(agents)} sessions · {number(sum(agent['span_count'] for agent in agents))} spans"))
    for service in snapshot.totals.get("services", []):
        if service not in known:
            rows.append((service, service, "OTLP / imported"))
    return rows


def event_rows(snapshot):
    rows = []
    detail = snapshot.detail
    for event in detail.get("progress", []):
        rows.append((event.get("time_ns", 0), event.get("stage", "Progress"), event.get("message", "")))
    for log in detail.get("logs", [])[:30]:
        body = log.get("body", "")
        rows.append((log.get("time_unix_nano", 0), log.get("severity_text") or log.get("event_name") or "LOG",
                     body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)))
    for finding in snapshot.findings[:10]:
        rows.append((finding.get("time_unix_nano", 0), finding.get("severity", "finding").upper(), finding.get("title", "Review finding")))
    for span in detail.get("spans", []):
        if span.get("scope_name") == "observatory.local_agents":
            rows.append((span["end_unix_nano"], span["status"], span["name"]))
    rows.sort(key=lambda item: item[0], reverse=True)
    return [f"{time.strftime('%H:%M:%S', time.localtime(stamp/1e9)) if stamp else '--:--:--'} {label}  {body}" for stamp, label, body in rows]


def span_rows(snapshot):
    rows = []
    for span in snapshot.detail.get("spans", []):
        duration = (span["end_unix_nano"] - span["start_unix_nano"]) / 1e6
        timing = "  unknown" if span.get("attributes", {}).get("observatory.duration_unknown") else f"{duration:7.1f}ms"
        rows.append(f"{span['status'][:7]:7} {timing}  {span['name']}")
    return rows


def activity_rows(snapshot):
    rows = []
    for item in snapshot.activity:
        stamp = time.strftime('%H:%M:%S', time.localtime(item['time_unix_nano']/1e9))
        state = str(item.get('result') or 'observed').upper()
        rows.append(f"{stamp} {item['domain'][:7].upper():7} {state[:7]:7} {item['action']}  {item['subject']}")
    return rows


def wrapped(text, width):
    lines = []
    for line in str(text).splitlines():
        lines.extend(textwrap.wrap(line, width=max(10, width), replace_whitespace=False) or [''])
    return lines


def print_snapshot(feed, agent=None):
    feed.refresh()
    snapshot = feed.snapshot
    totals = snapshot.totals
    print("OBSERVATORY  " + feed.api.endpoint)
    if snapshot.local_status.get("enabled"):
        mode = "bounded, redacted content" if snapshot.local_status.get('capture_content') else "metadata only"
        print(f"Source: local Codex / Claude Code session logs · {snapshot.local_status.get('sources', 0)} files · {mode}")
    print(f"Runs {number(totals.get('runs'))}  Spans {number(totals.get('spans'))}  "
          f"Tokens {number(totals.get('input_tokens'))} in / {number(totals.get('output_tokens'))} out  "
          f"Errors {number(totals.get('errors'))}  Known cost ${totals.get('known_cost_usd', 0):.4f}")
    for title, rows in (
        ("CONNECTED AGENTS", [f"{label}  {state}" for _, label, state in agent_rows(snapshot)[1:]]),
        ("RECENT TRACES", [f"{item['status']:10} {trace_name(item)}  {number(item['input_tokens'])} in / {number(item['output_tokens'])} out" for item in snapshot.traces[:10]]),
        ("SELECTED TRACE / SPANS", span_rows(snapshot)[:10]),
        ("LIVE PROGRESS / ERRORS", event_rows(snapshot)[:8]),
        ("RECENT ACTIVITY", activity_rows(snapshot)[:12]),
        ("AGENT OUTPUT", list(agent.lines)[-5:] if agent else []),
    ):
        print("\n+-- " + title + " " + "-" * max(0, 66-len(title)))
        for line in rows or ["Waiting for telemetry. Press c in the cockpit for connection commands."]:
            print("| " + clipped(line, 100))
        print("+" + "-" * 72)
    if snapshot.error:
        print(snapshot.error)
        raise CockpitError(snapshot.error)


def open_terminal(command):
    if sys.platform == "darwin":
        script = tempfile.NamedTemporaryFile(mode="w", prefix="observatory-", suffix=".command", delete=False)
        with script:
            script.write("#!/bin/sh\nrm -- " + shlex.quote(script.name) + "\ncd " + shlex.quote(os.getcwd()) + "\nexec " + shlex.join(command) + "\n")
        os.chmod(script.name, 0o700)
        subprocess.Popen(["open", "-a", "Terminal", script.name])
        return
    if os.name == "nt":
        terminal = shutil.which("wt.exe")
        if terminal:
            subprocess.Popen([terminal, "--title", "Observatory", "--startingDirectory", os.getcwd(), *command])
            return
    elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        for executable, prefix in (("gnome-terminal", ["--working-directory", os.getcwd(), "--"]),
                                   ("konsole", ["--workdir", os.getcwd(), "-e"]),
                                   ("x-terminal-emulator", ["-e"]), ("xterm", ["-e"])):
            terminal = shutil.which(executable)
            if terminal:
                subprocess.Popen([terminal, *prefix, *command])
                return
    raise CockpitError("No graphical terminal is available. Run the cockpit in your current terminal, or use --once.")


def run_cockpit(*, db_path="observatory.db", port=14318, endpoint=None, service="my-agent", target=None,
                arguments=(), python=None, module=False, capture_content=False, once=False, new_window=False, local_agents=True, metadata_only=False):
    if not 1 <= port <= 65535:
        raise CockpitError("Choose a port between 1 and 65535.")
    if once and target:
        raise CockpitError("--once prints a snapshot. Omit the agent filename, or run the interactive cockpit to launch it.")
    if new_window:
        command = [sys.executable, "-m", "observatory", "--db", str(Path(db_path).resolve()), "cockpit", "--port", str(port), "--service", service]
        if endpoint:
            command.extend(["--endpoint", endpoint])
        if python:
            command.extend(["--python", python])
        if module:
            command.append("--module")
        if capture_content:
            command.append("--capture-content")
        if not local_agents:
            command.append("--no-local-agents")
        if metadata_only:
            command.append("--metadata-only")
        if once:
            command.append("--once")
        if target:
            command.extend([str(Path(target).resolve()) if not module else target, *arguments])
        open_terminal(command)
        return
    if not once and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise CockpitError("Run this command in an interactive terminal. Use --new-window to open one, or --once for a snapshot.")
    if not once:
        try:
            from .terminal_ui import ObservatoryApp
        except ImportError as exc:
            raise CockpitError("The terminal UI needs Textual. Reinstall the CLI, or install this checkout with: python -m pip install .") from exc
    token = os.environ.get("OBSERVATORY_TOKEN")
    receiver = agent = feed = opencode_process = None
    api = Api(endpoint or f"http://127.0.0.1:{port}", token)
    try:
        try:
            health = api.get("/api/health")
            if not health.get("ok"):
                raise CockpitError("The endpoint is not a ready Observatory receiver.")
            if metadata_only and health.get("local_agents", {}).get("capture_content"):
                raise CockpitError("The existing receiver captures session content. Restart it with serve --metadata-only, then reconnect.")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise CockpitError("Receiver requires authentication. Set OBSERVATORY_TOKEN before starting.") from exc
            raise CockpitError(f"The endpoint returned HTTP {exc.code}. Check the receiver address.") from exc
        except (urllib.error.URLError, OSError) as exc:
            if endpoint:
                raise CockpitError("Cannot connect to the receiver at " + api.endpoint) from exc
            try:
                opencode_process, opencode_url = start_local_opencode() if not once else (None, None)
                receiver = LocalReceiver(db_path, port, token, local_agents=local_agents,
                                         metadata_only=metadata_only, opencode_url=opencode_url)
            except OSError as bind_error:
                if opencode_process is not None:
                    opencode_process.terminate()
                    try:
                        opencode_process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        opencode_process.kill()
                raise CockpitError(f"Cannot start a local receiver on port {port}: {bind_error}") from bind_error
        feed = Feed(api)
        if target:
            try:
                agent = AgentProcess(target, arguments, api.endpoint, service, python or sys.executable, module, capture_content)
            except OSError as exc:
                raise CockpitError("Cannot launch the agent: " + str(exc)) from exc
        if once:
            print_snapshot(feed, agent)
        else:
            if urlsplit(api.endpoint).hostname in {"127.0.0.1", "localhost", "::1"}:
                try:
                    webbrowser.open(api.endpoint + "/control")
                except OSError:
                    pass
            ObservatoryApp(feed, agent, receiver is not None).run()
    except KeyboardInterrupt:
        pass
    finally:
        if agent is not None:
            agent.close()
        if feed is not None:
            if feed.thread.is_alive():
                feed.close()
        if receiver is not None:
            receiver.close()
        if opencode_process is not None and opencode_process.poll() is None:
            opencode_process.terminate()
            try:
                opencode_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                opencode_process.kill()
