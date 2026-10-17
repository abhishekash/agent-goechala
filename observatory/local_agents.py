"""Observe local agent session metadata without changing or restarting agents.

Session files are read-only. Content capture is bounded and redacted; metadata-only
mode omits task text, arguments, and results. Agent log formats vary by version.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

from .core import normalize_native
from .local_activity import mcp_attributes, mcp_failed

MAX_READ = 8 * 1024 * 1024
MAX_LINE = 2 * 1024 * 1024
MAX_SOURCES = 50


def identity(*parts, size=32):
    return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()[:size]


def timestamp(value):
    if isinstance(value, (int, float)):
        magnitude = abs(value)
        factor = 1 if magnitude >= 1e17 else 1000 if magnitude >= 1e14 else 1_000_000 if magnitude >= 1e11 else 1_000_000_000
        return max(0, int(value * factor))
    if isinstance(value, str):
        try:
            return max(0, int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1e9))
        except ValueError:
            pass
    return 0


def count(value):
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


class Session:
    def __init__(self, store, path, runtime, capture_details=True):
        self.store, self.path, self.runtime = store, Path(path), runtime
        self.service = runtime.lower().replace(" ", "-")
        self.source_id = identity(runtime, self.path.resolve())
        self.capture_details = capture_details
        self.internal = False
        self.session_id = self.path.stem
        self.offset = 0
        self.file_id = None
        self.pending = b""
        self.turn = "session"
        self.model = None
        self.project = ""
        self.working_directory = ""
        self.branch = ""
        self.run_state = "unknown"
        self.state_since = 0
        self.waiting_for = ""
        self.pending_tools = {}
        self.started = {}
        self.calls = {}
        self.usage = {}
        self.tasks = {}
        self.goal = ""
        self.tool_inputs = {}
        self.modern_usage = False
        self.last_total = None
        self.last_heartbeat = 0
        self.last_state = None
        self.skipped = 0
        self.last_activity = None

    def observe_state(self, row, stamp):
        """Track explicit lifecycle records; absence of output is not an approval wait."""
        payload = (row.get("payload") or {}) if self.runtime == "Codex" else row
        if not isinstance(payload, dict):
            return
        cwd = payload.get("cwd")
        if cwd:
            self.project = Path(cwd).name
            self.working_directory = str(cwd) if self.capture_details else ""
        git = payload.get("git") or {}
        branch = payload.get("gitBranch") or (git.get("branch") if isinstance(git, dict) else None)
        if branch and self.capture_details:
            self.branch = str(branch)
        state = self.run_state
        event = payload.get("type")
        if self.runtime == "Codex":
            if row.get("type") == "event_msg":
                if event in {"task_started", "task_complete", "task_aborted"}:
                    state = {"task_started": "running", "task_complete": "completed", "task_aborted": "interrupted"}[event]
                    self.pending_tools.clear()
                elif event in {"approval_request", "request_user_input"}:
                    state = "waiting"
                    self.waiting_for = "Approval" if event == "approval_request" else "User input"
            if row.get("type") == "response_item":
                key = str(payload.get("call_id") or payload.get("id"))
                if event in {"function_call", "custom_tool_call"}:
                    name = str(payload.get("name") or "Tool")
                    self.pending_tools[key] = name
                    state = "running"
                elif event in {"function_call_output", "custom_tool_call_output"}:
                    self.pending_tools.pop(key, None)
                    state = "running"
                    self.waiting_for = ""
        else:
            message = row.get("message") or {}
            if not isinstance(message, dict):
                return
            blocks = message.get("content")
            blocks = blocks if isinstance(blocks, list) else []
            if row.get("type") == "user" and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
                state = "running"
                self.pending_tools.clear()
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    self.pending_tools[str(block.get("id"))] = str(block.get("name") or "Tool")
                    state = "running"
                elif block.get("type") == "tool_result":
                    self.pending_tools.pop(str(block.get("tool_use_id")), None)
                    state = "running"
                    self.waiting_for = ""
            if message.get("stop_reason") == "end_turn":
                state = "completed"
                self.pending_tools.clear()
        if self.pending_tools:
            names = list(self.pending_tools.values())
            if any(name.rsplit(".", 1)[-1] in {"request_user_input", "AskUserQuestion"} for name in names):
                state, self.waiting_for = "waiting", "User input"
            else:
                mcp = mcp_attributes(names[-1])
                label = mcp["mcp.server.name"] + " / " + mcp["mcp.tool.name"] if mcp else names[-1]
                self.waiting_for = "Tool response: " + label
                if len(names) > 1:
                    self.waiting_for += f" (+{len(names)-1} pending)"
        elif state != "waiting":
            self.waiting_for = ""
        if state != self.run_state:
            self.state_since = stamp
        self.run_state = state

    def span(self, key, name, stamp, attrs=None, *, start=None, parent=True, error=False):
        if not stamp:
            return None
        attributes = {"session.id": self.session_id, "observatory.project": self.project,
                      "observatory.data_source": "local-session-log", **(attrs or {})}
        return normalize_native({
            "trace_id": identity(self.source_id, self.turn),
            "span_id": identity(self.source_id, key, size=16) if parent else identity(self.source_id, self.turn, "root", size=16),
            "parent_span_id": identity(self.source_id, self.turn, "root", size=16) if parent else None,
            "name": name, "start_unix_nano": min(start or stamp, stamp), "end_unix_nano": stamp,
            "status": "ERROR" if error else "OK", "attributes": attributes,
            "resource_attributes": {"service.name": self.service, "host.name": socket.gethostname(),
                "os.type": platform.system().lower(), "observatory.source_id": self.source_id,
                "observatory.capture": "local-session-log"},
            "scope_name": "observatory.local_agents",
        }, self.service)

    def root(self, stamp, state):
        attrs = {"agent.task": "Approval review" if self.internal else self.tasks.get(self.turn) or self.runtime + (" · " + self.project if self.project else "") + " · turn",
                 "agent.run.state": state}
        if state in {"completed", "interrupted"}:
            attrs["agent.stopped_reason"] = state
        return self.span((self.turn, "root"), "agent.run", stamp,
            attrs, start=self.started.get(self.turn), parent=False)

    def model_span(self, key, usage, stamp, *, claude=False):
        if not isinstance(usage, dict):
            return None
        input_tokens = count(usage.get("input_tokens"))
        if claude:
            input_tokens += count(usage.get("cache_read_input_tokens")) + count(usage.get("cache_creation_input_tokens"))
        attrs = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "anthropic" if claude else "openai",
                 "gen_ai.usage.input_tokens": input_tokens,
                 "gen_ai.usage.output_tokens": count(usage.get("output_tokens")),
                 "gen_ai.usage.cache_read.input_tokens": count(usage.get("cache_read_input_tokens") if claude else usage.get("cached_input_tokens")),
                 "observatory.duration_unknown": True}
        if self.model:
            attrs["gen_ai.request.model"] = str(self.model)[:200]
        return self.span(key, self.runtime + " · " + str(self.model or "model call")[:200], stamp, attrs)

    def codex(self, row, stamp):
        kind, payload = row.get("type"), row.get("payload") or {}
        if not isinstance(payload, dict):
            return []
        if kind == "session_meta":
            self.session_id = str(payload.get("id") or payload.get("session_id") or self.session_id)
            self.project = Path(payload["cwd"]).name if payload.get("cwd") else self.project
        elif kind == "turn_context":
            self.turn = str(payload.get("turn_id") or self.turn)
            self.model = payload.get("model") or self.model
            if self.model == "codex-auto-review" and not self.internal:
                self.internal = True
                self.capture_details = False
                self.service = "codex-review"
                self.tasks.clear()
                self.goal = ""
                self.store.redact_internal_source(self.source_id)
            self.project = Path(payload["cwd"]).name if payload.get("cwd") else self.project
        elif kind == "token_usage_record":
            self.modern_usage = True
            self.turn = str(payload.get("turn_id") or self.turn)
            key = payload.get("response_id") or row.get("ordinal") or row.get("timestamp")
            return [self.model_span(("model", key), payload.get("usage"), stamp)]
        elif kind == "response_item":
            event = payload.get("type")
            key = str(payload.get("call_id") or payload.get("id"))
            if event in {"function_call", "custom_tool_call"}:
                name = str(payload.get("name") or "Tool")
                if mcp_attributes(name):
                    self.calls[key] = (name, stamp, self.turn)
            elif event in {"function_call_output", "custom_tool_call_output"} and key in self.calls:
                name, start, turn = self.calls[key]
                previous, self.turn = self.turn, turn
                span = self.span(("tool", key), name, stamp,
                    {"gen_ai.operation.name": "execute_tool", "tool.name": name, **mcp_attributes(name)},
                    start=start, error=mcp_failed(payload.get("output")))
                self.turn = previous
                return [span]
        elif kind == "event_msg":
            event = payload.get("type")
            self.turn = str(payload.get("turn_id") or self.turn)
            if event == "task_started":
                self.started[self.turn] = timestamp(payload.get("started_at")) or stamp
                return [self.root(stamp, "running")]
            if event in {"task_complete", "task_aborted"}:
                return [self.root(stamp, "completed" if event == "task_complete" else "interrupted")]
            if event == "token_count" and not self.modern_usage:
                info = payload.get("info") or {}
                total = info.get("total_token_usage")
                if not isinstance(total, dict) or total == self.last_total:
                    return []
                usage = info.get("last_token_usage")
                self.last_total = total
                return [self.model_span(("usage", self.turn, json.dumps(total, sort_keys=True)), usage, stamp)]
            if event == "item_completed":
                item = payload.get("item") or {}
                tool_type = item.get("type")
                names = {"CommandExecution": "Shell command", "FileChange": "File changes",
                         "McpToolCall": "MCP tool", "Extension": "Extension tool"}
                if tool_type in names:
                    attrs = {"gen_ai.operation.name": "execute_tool", "tool.name": names[tool_type]}
                    if tool_type == "McpToolCall":
                        attrs["tool.name"] = str(item.get("tool") or "MCP tool")[:128]
                        attrs["mcp.tool.name"] = attrs["tool.name"]
                        attrs["mcp.server.name"] = str(item.get("server") or "")[:128]
                        attrs["mcp.method.name"] = "tools/call"
                    code = item.get("exit_code")
                    if isinstance(code, int):
                        attrs["process.exit.code"] = code
                    error = item.get("status") in {"failed", "error"} or isinstance(code, int) and code != 0 or (tool_type == "McpToolCall" and mcp_failed(item.get("result")))
                    end = timestamp(payload.get("completed_at_ms")) or stamp
                    start = timestamp(payload.get("started_at_ms"))
                    from .local_activity import duration
                    elapsed = duration(item)
                    if not start and elapsed is not None:
                        start = max(0, end-int(elapsed*1e6))
                    if not start:
                        attrs["observatory.duration_unknown"] = True
                    name = (attrs["mcp.server.name"] + " / " + attrs["tool.name"]) if tool_type == "McpToolCall" else str(attrs["tool.name"])
                    return [self.span(("tool", item.get("id") or row.get("ordinal") or stamp),
                        name, end, attrs, start=start, error=error)]
        return []

    def claude(self, row, stamp):
        self.session_id = str(row.get("sessionId") or self.session_id)
        self.project = Path(row["cwd"]).name if row.get("cwd") else self.project
        message = row.get("message") or {}
        if not isinstance(message, dict):
            return []
        content = message.get("content") or []
        blocks = content if isinstance(content, list) else []
        result = []
        if row.get("type") == "user":
            tools = [block for block in blocks if isinstance(block, dict) and block.get("type") == "tool_result"]
            if not tools:
                self.turn = str(row.get("uuid") or stamp)
                self.started[self.turn] = stamp
                result.append(self.root(stamp, "running"))
            for block in tools:
                key = block.get("tool_use_id")
                name, start, turn = self.calls.get(key, ("Claude tool", stamp, self.turn))
                previous, self.turn = self.turn, turn
                result.append(self.span(("tool", key), name, stamp,
                    {"gen_ai.operation.name": "execute_tool", "tool.name": name, **mcp_attributes(name)},
                    start=start, error=bool(block.get("is_error"))))
                self.turn = previous
        elif row.get("type") == "assistant":
            self.model = message.get("model") or self.model
            key = message.get("id") or row.get("uuid") or stamp
            usage = message.get("usage")
            if isinstance(usage, dict):
                previous = self.usage.setdefault(key, {})
                for field in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                    previous[field] = max(count(previous.get(field)), count(usage.get(field)))
                result.append(self.model_span(("model", key), previous, stamp, claude=True))
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self.calls[block.get("id")] = (str(block.get("name") or "Claude tool"), stamp, self.turn)
            if message.get("stop_reason") == "end_turn":
                result.append(self.root(stamp, "completed"))
        return result

    def poll(self):
        stat = self.path.stat()
        file_id = stat.st_dev, stat.st_ino
        if self.file_id != file_id or stat.st_size < self.offset:
            self.offset, self.pending = 0, b""
            self.file_id = file_id
            self.last_total = None
            self.started.clear()
            self.calls.clear()
            self.usage.clear()
            self.tasks.clear()
            self.goal = ""
            self.tool_inputs.clear()
            self.pending_tools.clear()
            self.run_state, self.waiting_for, self.state_since = "unknown", "", 0
        with self.path.open("rb") as stream:
            if self.offset == 0 and stat.st_size > MAX_READ:
                # Recover session identity from the header, then import a bounded tail.
                header = stream.readline(MAX_LINE)
                try:
                    row = json.loads(header)
                    if self.runtime == "Codex":
                        self.codex(row, timestamp(row.get("timestamp")))
                except (ValueError, TypeError, AttributeError):
                    pass
                stream.seek(stat.st_size - MAX_READ)
                stream.readline()
                self.offset = stream.tell()
            stream.seek(self.offset)
            data = stream.read(MAX_READ)
            self.offset = stream.tell()
        chunks = (self.pending + data).split(b"\n")
        self.pending = chunks.pop()
        if len(self.pending) > MAX_LINE:
            self.pending = b""
            self.skipped += 1
        records = []
        for line in chunks:
            if len(line) > MAX_LINE:
                self.skipped += 1
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    records.append(row)
            except (ValueError, UnicodeDecodeError):
                self.skipped += 1
        if self.runtime == "Codex" and any(row.get("type") == "token_usage_record" for row in records):
            self.modern_usage = True
        spans = []
        logs = []
        from .local_activity import codex_activity, claude_activity
        for row in records:
            try:
                stamp = timestamp(row.get("timestamp"))
                spans.extend(span for span in (self.codex(row, stamp) if self.runtime == "Codex" else self.claude(row, stamp)) if span)
                logs.extend(codex_activity(self, row, stamp) if self.runtime == "Codex" else claude_activity(self, row, stamp))
                self.observe_state(row, stamp)
                if self.capture_details and self.turn in self.tasks and self.turn in self.started:
                    payload = row.get("payload") or {}
                    if (self.runtime == "Codex" and isinstance(payload, dict) and (payload.get("item") or {}).get("type") == "UserMessage") or (self.runtime == "Claude Code" and row.get("type") == "user" and plaintext_task(row)):
                        spans.append(self.root(stamp, "running"))
            except (ValueError, TypeError, KeyError, AttributeError):
                self.skipped += 1
        if self.internal:
            for span in spans:
                span['service'] = self.service
                span['resource_attributes']['service.name'] = self.service
                if span['name'] == 'agent.run':
                    span['attributes']['agent.task'] = 'Approval review'
            for log in logs:
                log['service'] = self.service
                log['body'] = ''
                log['attributes']['agent.activity.subject'] = 'Approval review'
        if spans:
            self.store.ingest(spans)
        if logs:
            self.store.ingest_logs(logs)
            last = logs[-1]
            self.last_activity = {"time_unix_nano": last["time_unix_nano"],
                                  "domain": last["attributes"].get("agent.activity.domain"),
                                  "action": last["attributes"].get("agent.activity.action"),
                                  "subject": last["attributes"].get("agent.activity.subject"),
                                  "result": last["attributes"].get("agent.activity.result")}
        state = "running" if time.time() - stat.st_mtime < 35 else "stopped"
        if time.monotonic() - self.last_heartbeat > 10 or state != self.last_state:
            self.store.register_connection({"source_id": self.source_id, "service": self.service,
                "runtime": self.runtime + " local sessions", "integrations": ["local-session-log"],
                "capture_content": self.capture_details, "state": state})
            self.last_heartbeat, self.last_state = time.monotonic(), state
        return len(spans)


def plaintext_task(row):
    message = row.get("message") or {}
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    return isinstance(content, str) or isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "text" for block in content)


class LocalAgents:
    def __init__(self, store, capture_details=True):
        self.store = store
        self.capture_details = capture_details
        self.stop = threading.Event()
        self.sessions = {}
        self.errors = 0
        self.runtime_snapshot = {}
        self.runtime_updated = 0
        self.thread = threading.Thread(target=self.loop, name="observatory-local-agents", daemon=True)

    def discover(self):
        roots = [(Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex")))/"sessions", "Codex"),
                 (Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home()/".claude")))/"projects", "Claude Code")]
        recent = []
        for root, runtime in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*.jsonl"):
                try:
                    modified = path.stat().st_mtime
                    if modified >= time.time() - 86400:
                        recent.append((modified, path, runtime))
                except OSError:
                    self.errors += 1
        selected = sorted(recent, key=lambda row: row[0], reverse=True)[:MAX_SOURCES]
        keys = {path for _, path, _ in selected}
        self.sessions = {path: session for path, session in self.sessions.items() if path in keys}
        for _, path, runtime in selected:
            if path not in self.sessions:
                self.sessions[path] = Session(self.store, path, runtime, capture_details=self.capture_details)

    def poll(self):
        accepted = 0
        for session in list(self.sessions.values()):
            try:
                accepted += session.poll()
            except OSError:
                self.errors += 1
        return accepted

    def loop(self):
        discovered = 0
        while not self.stop.is_set():
            try:
                if time.monotonic() - discovered >= 10:
                    self.discover()
                    discovered = time.monotonic()
                self.poll()
            except Exception:
                self.errors += 1
            self.stop.wait(1)

    def start(self):
        self.thread.start()
        return self

    def status(self):
        sessions = list(self.sessions.values())
        if time.monotonic() - self.runtime_updated >= 5:
            from .local_runtime import snapshot
            self.runtime_snapshot = snapshot()
            self.runtime_updated = time.monotonic()
        return {"enabled": True, "sources": len(sessions), "read_errors": self.errors,
                "skipped_records": sum(session.skipped for session in sessions),
                "capture_content": self.capture_details, "lookback_hours": 24, "max_sources": MAX_SOURCES,
                "runtime": self.runtime_snapshot,
                "sessions": [{"source_id": session.source_id, "runtime": session.runtime,
                              "service": session.service, "session_id": session.session_id,
                              "project": session.project, "model": session.model,
                              "working_directory": session.working_directory, "branch": session.branch,
                              "run_state": session.run_state, "state_since_ns": session.state_since,
                              "waiting_for": session.waiting_for,
                              "task": "Approval review" if session.internal else (session.tasks.get(session.turn) or "")[:500],
                              "goal": "" if session.internal else session.goal[:4000],
                              "capture_content": session.capture_details,
                              "last_activity": session.last_activity,
                              "bytes_read": session.offset} for session in sessions]}

    def close(self):
        self.stop.set()
        self.thread.join(timeout=15)
