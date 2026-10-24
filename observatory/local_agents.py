"""Observe local agent session metadata without changing or restarting agents.

Session files are read-only. Content capture is bounded and redacted; metadata-only
mode omits task text, arguments, and results. Agent log formats vary by version.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import platform
import socket
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path

from .core import normalize_native
from .local_activity import mcp_attributes, mcp_failed, plaintext
from .local_state import RecentMap
from .native_json import loads

MAX_READ = 8 * 1024 * 1024
MAX_LINE = 2 * 1024 * 1024
MAX_SOURCES = 50
MAX_CONFIG_BYTES = 1024 * 1024
MAX_RECENT_TURNS = 64
MAX_RECENT_CALLS = 512
MAX_RECENT_USAGE = 256
CLAUDE_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def identity(*parts, size=32):
    return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()[:size]


def timestamp(value):
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return 0
        magnitude = abs(value)
        factor = 1 if magnitude >= 1e17 else 1000 if magnitude >= 1e14 else 1_000_000 if magnitude >= 1e11 else 1_000_000_000
        stamp = max(0, int(value * factor))
        return stamp if stamp <= 2**63 - 1 else 0
    if isinstance(value, str):
        try:
            stamp = max(0, int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1e9))
            return stamp if stamp <= 2**63 - 1 else 0
        except (ValueError, OverflowError, OSError):
            pass
    return 0


def count(value):
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def _server_names(data, field):
    """Return configured MCP server names without exposing their settings."""
    servers = data.get(field) if isinstance(data, dict) else None
    # OpenCode v1 places names directly under `mcp`; v2 nests them in
    # `mcp.servers`. Accept both so the inventory follows the user's config.
    if field == "mcp" and isinstance(servers, dict) and isinstance(servers.get("servers"), dict):
        servers = servers["servers"]
    if not isinstance(servers, dict):
        return []
    return [str(name).strip()[:128] for name, value in servers.items()
            if isinstance(name, str) and name.strip() and isinstance(value, dict)][:500]


def _decode_json_or_jsonc(raw, jsonc=False):
    """Parse JSONC without stripping comment markers inside quoted strings."""
    text = raw.decode("utf-8")
    if jsonc:
        output = []
        index = 0
        quoted = escaped = False
        while index < len(text):
            char = text[index]
            following = text[index + 1] if index + 1 < len(text) else ""
            if quoted:
                output.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                index += 1
                continue
            if char == '"':
                quoted = True
                output.append(char)
                index += 1
            elif char == "/" and following == "/":
                index += 2
                while index < len(text) and text[index] not in "\r\n":
                    index += 1
            elif char == "/" and following == "*":
                end = text.find("*/", index + 2)
                if end < 0:
                    raise ValueError("unterminated JSONC comment")
                output.extend(" " for item in text[index:end + 2] if item in "\r\n")
                index = end + 2
            else:
                output.append(char)
                index += 1
        text = "".join(output)
        # OpenCode's JSONC examples permit trailing commas.
        output = []
        index = 0
        quoted = escaped = False
        while index < len(text):
            char = text[index]
            if quoted:
                output.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
                output.append(char)
            elif char == ",":
                lookahead = index + 1
                while lookahead < len(text) and text[lookahead].isspace():
                    lookahead += 1
                if lookahead == len(text) or text[lookahead] not in "]}":
                    output.append(char)
            else:
                output.append(char)
            index += 1
        text = "".join(output)
    return json.loads(text)


def discover_mcp_configurations(global_paths=None, project_directories=(), on_error=None):
    """Read known client config files and return names only, never server values."""
    home = Path.home()
    paths = list(global_paths) if global_paths is not None else [
        (Path(os.environ.get("CODEX_HOME", home / ".codex")) / "config.toml", "Codex", "User"),
        (home / ".claude.json", "Claude Code", "User"),
        (Path(os.environ.get("CURSOR_CONFIG_DIR", home / ".cursor")) / "mcp.json", "Cursor", "User"),
        (Path(os.environ.get("GEMINI_CLI_HOME", home / ".gemini")) / "settings.json", "Gemini CLI", "User"),
        (Path(os.environ.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH", "/etc/gemini-cli/settings.json")),
         "Gemini CLI", "System"),
        (Path(os.environ.get("COPILOT_HOME", home / ".copilot")) / "mcp-config.json",
         "GitHub Copilot CLI", "User"),
    ]
    if global_paths is None:
        config_file = os.environ.get("OPENCODE_CONFIG")
        xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        config_dir = Path(os.environ.get("OPENCODE_CONFIG_DIR") or (xdg_config_home / "opencode"))
        if config_file:
            paths.append((Path(config_file), "OpenCode", "User"))
        else:
            paths.extend((config_dir / filename, "OpenCode", "User")
                         for filename in ("opencode.json", "opencode.jsonc"))
    candidates = [(Path(path), client, scope) for path, client, scope in paths]
    for directory in project_directories:
        if not directory:
            continue
        root = Path(directory)
        candidates.extend([
            (root / ".mcp.json", "Claude Code", "Project"),
            (root / ".cursor" / "mcp.json", "Cursor", "Project"),
            (root / ".vscode" / "mcp.json", "VS Code", "Project"),
            (root / ".gemini" / "settings.json", "Gemini CLI", "Project"),
            (root / "opencode.json", "OpenCode", "Project"),
            (root / "opencode.jsonc", "OpenCode", "Project"),
            (root / ".opencode" / "opencode.json", "OpenCode", "Project"),
            (root / ".opencode" / "opencode.jsonc", "OpenCode", "Project"),
        ])

    found, seen = [], set()
    for path, client, scope in candidates:
        try:
            if not path.is_file():
                continue
            if path.stat().st_size > MAX_CONFIG_BYTES:
                if on_error:
                    on_error()
                continue
            raw = path.read_bytes()
            if path.suffix.lower() == ".toml":
                data = tomllib.loads(raw.decode("utf-8"))
                field = "mcp_servers"
            else:
                data = _decode_json_or_jsonc(raw, path.suffix.lower() == ".jsonc")
                field = ("servers" if client == "VS Code" else
                         "mcp" if client == "OpenCode" else "mcpServers")
            for name in _server_names(data, field):
                key = (client, scope, name)
                if key in seen:
                    continue
                seen.add(key)
                found.append({"client": client, "scope": scope, "server": name,
                              "config": path.name})
        except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError):
            if on_error:
                on_error()
            continue
    return sorted(found, key=lambda item: (item["client"].casefold(), item["scope"], item["server"].casefold()))


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
        self.discarding_line = False
        self.turn = "session"
        self.model = None
        self.provider = None
        self.project = ""
        self.working_directory = ""
        self.branch = ""
        self.run_state = "unknown"
        self.state_since = 0
        self.waiting_for = ""
        self.pending_tools = RecentMap(MAX_RECENT_CALLS)
        self.started = RecentMap(MAX_RECENT_TURNS)
        self.calls = RecentMap(MAX_RECENT_CALLS)
        self.usage = RecentMap(MAX_RECENT_USAGE)
        self.tasks = RecentMap(MAX_RECENT_TURNS)
        self.tool_inputs = RecentMap(MAX_RECENT_CALLS)
        self.modern_usage = False
        self.last_total = None
        self.last_heartbeat = 0
        self.last_state = None
        self.skipped = 0
        self.last_activity = None
        self.last_success_ns = 0
        self.backlog_bytes = 0
        self.schema_errors = 0
        self.skipped_oversized = 0
        self.source_size_bytes = 0
        self.source_mtime_ns = 0
        self.records_last_poll = 0
        self.spans_last_poll = 0
        self.logs_last_poll = 0
        self.bytes_read_total = 0
        self.records_total = 0
        self.spans_total = 0
        self.logs_total = 0

    def finish_turn(self):
        # Roots and model usage are durable in Store. Keep only the current
        # prompt/start for status; unresolved calls have their own bounded grace
        # window and must survive completion and subsequent turns.
        for key in list(self.started):
            if key != self.turn:
                del self.started[key]
        if self.turn in self.tasks:
            task = self.tasks[self.turn]
            self.tasks.clear()
            self.tasks[self.turn] = task
        else:
            self.tasks.clear()

    def diagnostics(self):
        return {"source_path": str(self.path), "last_success_ns": self.last_success_ns,
                "backlog_bytes": self.backlog_bytes, "skipped_records": self.skipped,
                "source_size_bytes": self.source_size_bytes, "source_mtime_ns": self.source_mtime_ns,
                "records_last_poll": self.records_last_poll,
                "spans_last_poll": self.spans_last_poll, "logs_last_poll": self.logs_last_poll,
                "bytes_read_total": self.bytes_read_total, "records_total": self.records_total,
                "spans_total": self.spans_total, "logs_total": self.logs_total,
                "bytes_read": self.offset,
                "skipped_oversized_records": self.skipped_oversized,
                "schema_errors": self.schema_errors,
                "cache_evictions": sum(mapping.evictions for mapping in
                    (self.started, self.tasks, self.calls, self.tool_inputs, self.usage, self.pending_tools)),
                "cache_tracked_bytes": sum(mapping.tracked_bytes for mapping in
                    (self.started, self.tasks, self.calls, self.tool_inputs, self.usage, self.pending_tools)),
                "cache_max_bytes": sum(mapping.max_bytes for mapping in
                    (self.started, self.tasks, self.calls, self.tool_inputs, self.usage, self.pending_tools)),
                "cache_dropped_oversized_entries": sum(mapping.dropped_oversized for mapping in
                    (self.started, self.tasks, self.calls, self.tool_inputs, self.usage, self.pending_tools))}

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
        elif self.runtime == "Pi":
            message = row.get("message") or {}
            if not isinstance(message, dict):
                return
            role = message.get("role")
            if role == "user":
                state = "running"
                self.pending_tools.clear()
            elif role == "assistant":
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        self.pending_tools[str(block.get("id") or "")] = str(block.get("name") or "Tool")
                if message.get("stopReason") == "stop":
                    state = "completed"
                    self.pending_tools.clear()
                else:
                    state = "running"
            elif role == "toolResult":
                self.pending_tools.pop(str(message.get("toolCallId") or ""), None)
                state = "running"
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
        attrs = {"gen_ai.operation.name": "chat",
                 "gen_ai.provider.name": "anthropic" if claude else "openai",
                 "observatory.duration_unknown": True}
        input_sources = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens") if claude else ("input_tokens",)
        if any(name in usage for name in input_sources):
            attrs["gen_ai.usage.input_tokens"] = sum(count(usage.get(name)) for name in input_sources)
        if "output_tokens" in usage:
            attrs["gen_ai.usage.output_tokens"] = count(usage.get("output_tokens"))
        cache_read = "cache_read_input_tokens" if claude else (
            "cached_input_tokens" if "cached_input_tokens" in usage else "cache_read_input_tokens")
        if cache_read in usage:
            attrs["gen_ai.usage.cache_read.input_tokens"] = count(usage.get(cache_read))
        cache_write = "cache_creation_input_tokens" if claude else (
            "cache_write_input_tokens" if "cache_write_input_tokens" in usage else "cache_creation_input_tokens")
        if cache_write in usage:
            attrs["gen_ai.usage.cache_write.input_tokens"] = count(usage.get(cache_write))
        reasoning = "reasoning_output_tokens" if "reasoning_output_tokens" in usage else "reasoning_tokens"
        if reasoning in usage:
            attrs["gen_ai.usage.reasoning_tokens"] = count(usage.get(reasoning))
        if self.model:
            attrs["gen_ai.request.model"] = str(self.model)[:200]
        if claude:
            attrs["observatory.native_usage"] = {field: count(usage.get(field)) for field in CLAUDE_USAGE_FIELDS}
        return self.span(key, self.runtime + " · " + str(self.model or "model call")[:200], stamp, attrs)

    def pi(self, row, stamp):
        kind = row.get("type")
        if kind == "session":
            self.session_id = str(row.get("id") or self.session_id)
            cwd = row.get("cwd")
            if cwd:
                self.project = Path(cwd).name
                self.working_directory = str(cwd) if self.capture_details else ""
            return []
        if kind == "model_change":
            self.model = row.get("modelId") or self.model
            self.provider = row.get("provider") or self.provider
            return []
        if kind != "message":
            return []
        message = row.get("message") or {}
        if not isinstance(message, dict):
            return []
        role = message.get("role")
        if role == "user":
            self.turn = str(row.get("id") or stamp or self.turn)
            self.started[self.turn] = stamp
            text = plaintext(message.get("content")) if self.capture_details else ""
            if text:
                self.tasks[self.turn] = text[:4000]
            return [self.root(stamp, "running")]
        if role == "assistant":
            self.model = message.get("model") or self.model
            self.provider = message.get("provider") or self.provider
            result = []
            usage = message.get("usage")
            if isinstance(usage, dict):
                usage_fields = {
                    "input": "input_tokens", "output": "output_tokens",
                    "cacheRead": "cache_read_input_tokens", "cacheWrite": "cache_write_input_tokens",
                    "reasoning": "reasoning_tokens",
                }
                normalized = {target: usage[source] for source, target in usage_fields.items()
                              if source in usage}
                span = self.model_span(("pi-model", row.get("id") or stamp), normalized, stamp)
                if span:
                    span["attributes"]["gen_ai.provider.name"] = str(self.provider or "unknown")[:128]
                    result.append(span)
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "toolCall":
                    continue
                call_id = str(block.get("id") or "")
                name = str(block.get("name") or "Tool")
                if call_id:
                    self.calls[call_id] = (name, stamp, self.turn)
            if message.get("stopReason") == "stop":
                result.append(self.root(stamp, "completed"))
            elif message.get("stopReason") == "error":
                result.append(self.root(stamp, "interrupted"))
            return result
        if role == "toolResult":
            call_id = str(message.get("toolCallId") or "")
            pending = self.calls.pop(call_id, None)
            name = str(message.get("toolName") or (pending[0] if pending else "Tool"))
            start = pending[1] if pending else stamp
            turn = pending[2] if pending else self.turn
            previous, self.turn = self.turn, turn
            span = self.span(("tool", call_id), name, stamp,
                {"gen_ai.operation.name": "execute_tool", "tool.name": name},
                start=start, error=bool(message.get("isError")))
            self.turn = previous
            return [span] if span else []
        return []

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

    def claude(self, row, stamp, prior_models=None):
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
                previous = self.usage.get(key)
                if previous is None:
                    previous = {}
                    # Evicting maxima must not lower a persisted model span if
                    # an old streaming update arrives after the cache window.
                    if self.usage.evictions or self.usage.dropped_oversized:
                        span_id = identity(self.source_id, ("model", key), size=16)
                        span = (prior_models or {}).get((identity(self.source_id, self.turn), span_id))
                        if span:
                            previous = span["attributes"].get("observatory.native_usage", previous)
                        elif hasattr(self.store, "_connect"):
                            with self.store._connect() as db:
                                saved = db.execute("SELECT attributes FROM spans WHERE trace_id=? AND span_id=?",
                                    (identity(self.source_id, self.turn), span_id)).fetchone()
                            if saved:
                                previous = json.loads(saved[0]).get("observatory.native_usage", {})
                for field in CLAUDE_USAGE_FIELDS:
                    previous[field] = max(count(previous.get(field)), count(usage.get(field)))
                self.usage[key] = previous
                result.append(self.model_span(("model", key), previous, stamp, claude=True))
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self.calls[block.get("id")] = (str(block.get("name") or "Claude tool"), stamp, self.turn)
            if message.get("stop_reason") == "end_turn":
                result.append(self.root(stamp, "completed"))
        return result

    def poll(self):
        stat = self.path.stat()
        self.source_size_bytes = stat.st_size
        self.source_mtime_ns = stat.st_mtime_ns
        self.records_last_poll = self.spans_last_poll = self.logs_last_poll = 0
        file_id = stat.st_dev, stat.st_ino
        if self.file_id != file_id or stat.st_size < self.offset:
            self.offset, self.pending = 0, b""
            self.discarding_line = False
            self.file_id = file_id
            self.last_total = None
            self.modern_usage = False
            self.started.clear()
            self.calls.clear()
            self.usage.clear()
            self.tasks.clear()
            self.tool_inputs.clear()
            self.pending_tools.clear()
            self.run_state, self.waiting_for, self.state_since = "unknown", "", 0
        with self.path.open("rb") as stream:
            if self.offset == 0 and stat.st_size > MAX_READ:
                # Recover session identity from the header, then import a bounded tail.
                header = stream.readline(MAX_LINE)
                try:
                    row = loads(header)
                    if self.runtime == "Codex":
                        self.codex(row, timestamp(row.get("timestamp")))
                except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
                    self.skipped += 1
                    self.schema_errors += 1
                stream.seek(stat.st_size - MAX_READ)
                # Skip only the partial first record, without allocating an
                # arbitrarily large line from a malformed session file.
                while True:
                    fragment = stream.readline(MAX_LINE)
                    if not fragment or fragment.endswith(b"\n"):
                        break
                self.offset = stream.tell()
            stream.seek(self.offset)
            data = stream.read(MAX_READ)
            self.offset = stream.tell()
        self.bytes_read_total += len(data)
        self.backlog_bytes = max(0, stat.st_size - self.offset)
        if self.discarding_line:
            _, separator, data = data.partition(b"\n")
            self.discarding_line = not bool(separator)
        chunks = (self.pending + data).split(b"\n")
        self.pending = chunks.pop()
        if len(self.pending) > MAX_LINE:
            self.pending = b""
            self.discarding_line = True
            self.skipped += 1
            self.skipped_oversized += 1
        records = []
        for line in chunks:
            if len(line) > MAX_LINE:
                self.skipped += 1
                self.skipped_oversized += 1
                continue
            try:
                row = loads(line)
                if isinstance(row, dict):
                    records.append(row)
                else:
                    self.skipped += 1
                    self.schema_errors += 1
            except (ValueError, UnicodeDecodeError, OverflowError, RecursionError):
                self.skipped += 1
                self.schema_errors += 1
        if self.runtime == "Codex" and any(row.get("type") == "token_usage_record" for row in records):
            self.modern_usage = True
        self.records_last_poll = len(records)
        self.records_total += len(records)
        spans = []
        # Poll-local references only (bounded by MAX_READ), discarded on return.
        # Avoid scanning the entire batch for late streaming usage updates.
        prior_models = {}
        logs = []
        from .local_activity import codex_activity, claude_activity
        for row in records:
            try:
                stamp = timestamp(row.get("timestamp"))
                if self.runtime == "Codex":
                    new_spans = self.codex(row, stamp)
                elif self.runtime == "Pi":
                    new_spans = self.pi(row, stamp)
                else:
                    new_spans = self.claude(row, stamp, prior_models)
                for span in new_spans:
                    if span:
                        spans.append(span)
                        if "observatory.native_usage" in span["attributes"]:
                            prior_models[(span["trace_id"], span["span_id"])] = span
                if self.runtime == "Codex":
                    logs.extend(codex_activity(self, row, stamp))
                elif self.runtime == "Pi":
                    from .local_activity import pi_activity
                    logs.extend(pi_activity(self, row, stamp))
                else:
                    logs.extend(claude_activity(self, row, stamp))
                self.observe_state(row, stamp)
                if self.capture_details and self.turn in self.tasks and self.turn in self.started:
                    payload = row.get("payload") or {}
                    if (self.runtime == "Codex" and isinstance(payload, dict) and (payload.get("item") or {}).get("type") == "UserMessage") or (self.runtime == "Claude Code" and row.get("type") == "user" and plaintext_task(row)):
                        spans.append(self.root(stamp, "running"))
                if self.run_state in {"completed", "interrupted"}:
                    self.finish_turn()
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
                self.skipped += 1
                self.schema_errors += 1
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
        self.spans_last_poll = len(spans)
        self.logs_last_poll = len(logs)
        self.spans_total += len(spans)
        self.logs_total += len(logs)
        self.last_success_ns = time.time_ns()
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
        self.discovered_sources = 0
        self.omitted_sources = 0
        self.errors = 0
        self.runtime_snapshot = {}
        self.runtime_updated = 0
        self.mcp_configurations = []
        self.mcp_config_errors = 0
        self.mcp_config_updated = 0
        from .opencode import OpenCodeReader
        self.opencode = OpenCodeReader(store, capture_details=capture_details)
        self.thread = threading.Thread(target=self.loop, name="observatory-local-agents", daemon=True)

    def discover(self):
        roots = [(Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex")))/"sessions", "Codex"),
                 (Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home()/".claude")))/"projects", "Claude Code"),
                 (Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home()/".pi"/"agent")))/"sessions", "Pi")]
        cutoff = time.time() - 86400
        self.discovered_sources = 0
        def recent():
            for root, runtime in roots:
                if not root.is_dir():
                    continue
                for path in root.rglob("*.jsonl"):
                    try:
                        modified = path.stat().st_mtime
                        if modified >= cutoff:
                            self.discovered_sources += 1
                            yield modified, path, runtime
                    except OSError:
                        self.errors += 1
        selected = heapq.nlargest(MAX_SOURCES, recent(), key=lambda row: row[0])
        self.omitted_sources = max(0, self.discovered_sources - len(selected))
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
                    project_directories = {session.working_directory for session in self.sessions.values()
                                           if session.working_directory}
                    from .local_runtime import project_directories as runtime_project_directories
                    project_directories.update(runtime_project_directories({"gemini", "cursor", "opencode"}))
                    config_errors = [0]
                    def note_config_error():
                        config_errors[0] += 1
                    self.mcp_configurations = discover_mcp_configurations(
                        project_directories=project_directories,
                        on_error=note_config_error)
                    self.opencode.set_mcp_servers(
                        item["server"] for item in self.mcp_configurations
                        if item["client"] == "OpenCode")
                    self.mcp_config_errors = config_errors[0]
                    self.mcp_config_updated = time.monotonic()
                    discovered = time.monotonic()
                self.opencode.poll()
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
        return {"enabled": True, "sources": len(sessions), "discovered_sources": self.discovered_sources,
                "omitted_sources": self.omitted_sources, "read_errors": self.errors,
                "skipped_records": sum(session.skipped for session in sessions),
                "backlog_bytes": sum(session.backlog_bytes for session in sessions),
                "schema_errors": sum(session.schema_errors for session in sessions),
                "skipped_oversized_records": sum(session.skipped_oversized for session in sessions),
                "capture_content": self.capture_details, "lookback_hours": 24, "max_sources": MAX_SOURCES,
                "opencode_discovered_sessions": self.opencode.discovered_sessions,
                "opencode_omitted_sessions": self.opencode.omitted_sessions,
                "opencode_diagnostics": self.opencode.diagnostics(),
                "runtime": self.runtime_snapshot,
                "mcp_configurations": list(self.mcp_configurations),
                "mcp_config_errors": self.mcp_config_errors,
                "mcp_config_updated": self.mcp_config_updated,
                "sessions": [{"source_id": session.source_id, "runtime": session.runtime,
                              "service": session.service, "session_id": session.session_id,
                              "project": session.project, "model": session.model,
                              "working_directory": session.working_directory, "branch": session.branch,
                              "run_state": session.run_state, "state_since_ns": session.state_since,
                              "waiting_for": session.waiting_for,
                              "task": "Approval review" if session.internal else (session.tasks.get(session.turn) or "")[:500],
                              "capture_content": session.capture_details,
                              "last_activity": session.last_activity,
                              "diagnostics": session.diagnostics(),
                              "bytes_read": session.offset} for session in sessions] + self.opencode.status_sessions(),
                "opencode_read_errors": self.opencode.errors}

    def close(self):
        self.stop.set()
        self.thread.join(timeout=15)
