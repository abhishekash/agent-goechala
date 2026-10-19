"""Read-only OpenCode session capture from its local SQLite database."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .core import normalize_native
from .local_activity import mcp_attributes, safe
from .local_agents import identity, timestamp

MAX_SESSIONS = 50
MAX_MESSAGES_PER_SESSION = 30
MAX_PARTS_PER_MESSAGE = 200
LOOKBACK_NS = 24 * 60 * 60 * 1_000_000_000


def database_path():
    """Return OpenCode's configured database path without invoking its CLI."""
    override = os.environ.get("OPENCODE_DB")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            path = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode" / path
        return path
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data_home / "opencode" / "opencode.db"


def _json(value):
    if not isinstance(value, str):
        return value if isinstance(value, dict) else {}
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _model(value):
    model = _json(value)
    if model:
        return str(model.get("id") or model.get("modelID") or "")
    return str(value or "")[:200]


def _first_timestamp(*values):
    for value in values:
        stamp = timestamp(value)
        if stamp:
            return stamp
    return 0


def _opencode_mcp_attributes(tool_name, server_names):
    """Resolve OpenCode's ``server_tool`` IDs using configured server names.

    OpenCode prefixes MCP tool IDs with the server name. Since ordinary custom
    tools can also contain underscores, only configured, unambiguous server
    prefixes are accepted here.
    """
    sanitized = {}
    for server in server_names:
        server = str(server).strip()
        if not server:
            continue
        prefix = re.sub(r"[^A-Za-z0-9_]", "_", server)
        names = sanitized.setdefault(prefix, [])
        if server not in names:
            names.append(server)
    matches = [(prefix, names[0]) for prefix, names in sanitized.items()
               if len(names) == 1 and tool_name.startswith(prefix + "_")
               and len(tool_name) > len(prefix) + 1]
    if not matches:
        return {}
    # OpenCode allows MCP names to contain underscores. The longest configured
    # prefix identifies the server when one server name prefixes another.
    longest = max(len(prefix) for prefix, _ in matches)
    best = [(prefix, server) for prefix, server in matches if len(prefix) == longest]
    if len(best) != 1:
        return {}
    prefix, server = best[0]
    return {"mcp.server.name": server, "mcp.tool.name": tool_name[len(prefix) + 1:],
            "mcp.method.name": "tools/call"}


class OpenCodeReader:
    """Poll bounded session metadata and native tool records without mutating OpenCode."""

    def __init__(self, store, capture_details=True, path=None):
        self.store = store
        self.capture_details = capture_details
        self.path = Path(path) if path else database_path()
        self.source_id = identity("OpenCode", self.path.resolve())
        self.fingerprint = None
        self.sessions = []
        self.discovered_sessions = 0
        self.omitted_sessions = 0
        self.errors = 0
        self.updated_ns = 0
        self.mcp_servers = ()

    def set_mcp_servers(self, server_names):
        """Set OpenCode server names discovered from config, without settings."""
        servers = tuple(dict.fromkeys(str(name)[:128] for name in server_names if name))
        if servers != self.mcp_servers:
            self.mcp_servers = servers
            # Re-read persisted spans so newly discovered configuration can
            # enrich tool records that were initially ingested without it.
            self.fingerprint = None

    @staticmethod
    def _tables(db):
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    @staticmethod
    def _columns(db, table):
        return {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}

    def _fingerprint(self):
        try:
            main = self.path.stat()
            wal_path = Path(str(self.path) + "-wal")
            wal = wal_path.stat() if wal_path.exists() else None
            return (main.st_mtime_ns, main.st_size,
                    wal.st_mtime_ns if wal else 0, wal.st_size if wal else 0)
        except OSError:
            return None

    def status_sessions(self):
        """Refresh recency labels even when OpenCode has not written a new DB page."""
        now_ns = time.time_ns()
        result = []
        for item in self.sessions:
            entry = dict(item)
            updated = entry.pop("last_update_ns", 0)
            entry["run_state"] = "running" if updated and now_ns - updated < 35_000_000_000 else "idle"
            entry["state_since_ns"] = updated
            result.append(entry)
        return result

    def _plan_progress(self, db, tables, session_id, trace_id, root_span_id, updated_ns):
        """Convert OpenCode's durable todo checklist into a deduplicated progress event."""
        if "todo" not in tables:
            return None
        columns = self._columns(db, "todo")
        if not {"session_id", "status"} <= columns:
            return None
        selected = [name for name in ("content", "status", "position", "time_updated") if name in columns]
        order = ' ORDER BY "position"' if "position" in columns else ""
        rows = db.execute(f'SELECT {",".join(chr(34)+name+chr(34) for name in selected)} FROM "todo" WHERE "session_id"=?{order} LIMIT 100',
                          (session_id,)).fetchall()
        if not rows:
            return None
        todos = []
        for row in rows:
            status = str(row["status"] or "pending").casefold().replace("-", "_")
            content = safe(row["content"] or "", 500) if self.capture_details and "content" in row.keys() else ""
            todos.append((status, content))
        complete_statuses = {"completed", "complete", "done"}
        active_statuses = {"in_progress", "running", "active"}
        completed = sum(status in complete_statuses for status, _ in todos)
        active = sum(status in active_statuses for status, _ in todos)
        pending = len(todos) - completed - active
        percent = round(completed * 100 / len(todos), 2)
        message = f"Plan progress · {completed}/{len(todos)} complete · {active} in progress · {pending} pending"
        if self.capture_details:
            symbols = {"completed": "✓", "complete": "✓", "done": "✓",
                       "in_progress": "→", "running": "→", "active": "→"}
            lines = [f"{symbols.get(status, '·')} {content or 'Task not reported'}"
                     for status, content in todos]
            message += "\n" + "\n".join(lines)
        message = safe(message, 8000)
        state_key = json.dumps(todos, ensure_ascii=False, separators=(",", ":"))
        event_id = identity("opencode-plan", session_id, trace_id, state_key)
        stamp = max([updated_ns, *(_first_timestamp(row["time_updated"]) for row in rows
                                  if "time_updated" in row.keys())])
        return {"trace_id": trace_id, "span_id": root_span_id, "time_unix_nano": stamp,
                "service": "opencode", "severity_number": 9, "severity_text": "INFO",
                "event_name": "agent.progress", "body": message,
                "attributes": {"session.id": session_id, "agent.progress.stage": "Plan",
                    "agent.progress.message": message, "agent.progress.percent": percent,
                    "observatory.record_id": event_id},
                "resource_attributes": {"service.name": "opencode", "agent.runtime": "OpenCode",
                    "observatory.source_id": self.source_id, "observatory.capture": "local-session-database"},
                "scope_name": "observatory.local_agents"}

    def poll(self):
        fingerprint = self._fingerprint()
        if fingerprint is None:
            self.sessions = []
            self.discovered_sessions = 0
            self.omitted_sessions = 0
            return 0
        if fingerprint == self.fingerprint:
            return 0
        accepted = []
        progress_logs = []
        sessions = []
        try:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                db.execute("PRAGMA busy_timeout=250")
                tables = self._tables(db)
                if "session" not in tables:
                    self.discovered_sessions = 0
                    self.omitted_sessions = 0
                    self.fingerprint = fingerprint
                    return 0
                session_columns = self._columns(db, "session")
                required = {"id", "time_updated"}
                if not required <= session_columns:
                    self.fingerprint = fingerprint
                    return 0
                columns = [name for name in ("id", "project_id", "title", "directory", "time_created",
                            "time_updated", "agent", "model", "tokens_input", "tokens_output")
                           if name in session_columns]
                now_ns = time.time_ns()
                cutoff_ms = (now_ns - LOOKBACK_NS) // 1_000_000
                self.discovered_sessions = int(db.execute(
                    'SELECT COUNT(*) FROM "session" WHERE "time_updated">=?', (cutoff_ms,)
                ).fetchone()[0])
                self.omitted_sessions = max(0, self.discovered_sessions - MAX_SESSIONS)
                sql = f'SELECT {",".join(chr(34)+name+chr(34) for name in columns)} FROM "session" WHERE "time_updated">=? ORDER BY "time_updated" DESC LIMIT ?'
                rows = db.execute(sql, (cutoff_ms, MAX_SESSIONS)).fetchall()
                project_names = {}
                if "project" in tables:
                    project_columns = self._columns(db, "project")
                    if "id" in project_columns:
                        project_fields = [name for name in ("id", "name", "worktree") if name in project_columns]
                        for project in db.execute(f'SELECT {",".join(chr(34)+name+chr(34) for name in project_fields)} FROM "project"'):
                            project_names[project["id"]] = project["name"] or Path(project["worktree"] or "").name
                message_table = "message" if "message" in tables else "session_message" if "session_message" in tables else None
                message_columns = self._columns(db, message_table) if message_table else set()
                part_columns = self._columns(db, "part") if "part" in tables else set()
                for session in rows:
                    data = dict(session)
                    session_id = str(data["id"])
                    directory = str(data.get("directory") or "")
                    project = project_names.get(data.get("project_id")) or Path(directory).name or ""
                    updated = timestamp(data.get("time_updated"))
                    item = {"source_id": identity(self.source_id, session_id), "runtime": "OpenCode",
                            "service": "opencode", "session_id": session_id, "project": project,
                            "working_directory": directory if self.capture_details else "",
                            "model": _model(data.get("model")), "last_update_ns": updated,
                            "run_state": "running" if now_ns-updated < 35_000_000_000 else "idle",
                            "state_since_ns": updated, "waiting_for": "", "task": (str(data.get("title") or "")[:500]
                                if self.capture_details else ""), "capture_content": self.capture_details,
                            "last_activity": {"time_unix_nano": updated, "domain": "agent",
                                              "action": "session update", "subject": project or "OpenCode session",
                                              "result": "observed"}, "bytes_read": 0}
                    sessions.append(item)
                    read_table, read_columns = message_table, message_columns
                    if not read_table or not {"id", "session_id", "data"} <= read_columns:
                        continue
                    msg_fields = [name for name in ("id", "session_id", "time_created", "time_updated", "data") if name in read_columns]
                    order_by = ' ORDER BY "time_created" DESC' if "time_created" in read_columns else ""
                    messages = db.execute(f'SELECT {",".join(chr(34)+name+chr(34) for name in msg_fields)} FROM "{read_table}" WHERE "session_id"=?{order_by} LIMIT ?',
                                          (session_id, MAX_MESSAGES_PER_SESSION)).fetchall()
                    if not messages and read_table == "message" and "session_message" in tables:
                        v2_columns = self._columns(db, "session_message")
                        if {"id", "session_id", "data"} <= v2_columns:
                            read_table = "session_message"
                            read_columns = v2_columns
                            msg_fields = [name for name in ("id", "session_id", "type", "time_created", "time_updated", "data") if name in v2_columns]
                            order_by = ' ORDER BY "time_created" DESC' if "time_created" in read_columns else ""
                            messages = db.execute(f'SELECT {",".join(chr(34)+name+chr(34) for name in msg_fields)} FROM "{read_table}" WHERE "session_id"=?{order_by} LIMIT ?',
                                                  (session_id, MAX_MESSAGES_PER_SESSION)).fetchall()
                    latest_trace = latest_root_id = None
                    for message in messages:
                        message_data = _json(message["data"])
                        role = message_data.get("role") or (message["type"] if "type" in message.keys() else "")
                        if str(role) != "assistant":
                            continue
                        message_id = str(message["id"])
                        trace_id = identity("opencode-trace", session_id, message_id)
                        root_id = identity("opencode-root", session_id, message_id, size=16)
                        if latest_trace is None:
                            latest_trace, latest_root_id = trace_id, root_id
                        message_created = message["time_created"] if "time_created" in message.keys() else 0
                        message_updated = message["time_updated"] if "time_updated" in message.keys() else message_created
                        start_ns = _first_timestamp((message_data.get("time") or {}).get("created"), message_created)
                        end_ns = _first_timestamp((message_data.get("time") or {}).get("completed"), message_updated, message_created)
                        if not start_ns:
                            start_ns = end_ns
                        if end_ns < start_ns:
                            start_ns = end_ns
                        finish = str(message_data.get("finish") or "")
                        state = "error" if finish.casefold() in {"error", "failed"} else "completed"
                        task = str(data.get("title") or "")[:500] if self.capture_details else ""
                        root_attrs = {"session.id": session_id, "agent.run.state": state,
                                      "agent.task": task or "OpenCode session",
                                      "observatory.project": project}
                        root = normalize_native({"trace_id": trace_id, "span_id": root_id, "parent_span_id": None,
                            "name": "agent.run", "start_unix_nano": start_ns, "end_unix_nano": end_ns,
                            "status": "ERROR" if state == "error" else "OK", "attributes": root_attrs,
                            "resource_attributes": {"service.name": "opencode", "agent.runtime": "OpenCode",
                                "observatory.source_id": self.source_id, "observatory.capture": "local-session-database"},
                            "scope_name": "observatory.opencode"}, "opencode")
                        accepted.append(root)
                        model = _model(message_data.get("modelID") or data.get("model"))
                        tokens = message_data.get("tokens") if isinstance(message_data.get("tokens"), dict) else {}
                        model_id = None
                        if model or tokens:
                            model_attrs = {"session.id": session_id, "gen_ai.operation.name": "chat",
                                           "gen_ai.provider.name": str(message_data.get("providerID") or "unknown"),
                                           "gen_ai.request.model": model or "Not reported"}
                            for source, target in (("input", "gen_ai.usage.input_tokens"),
                                ("output", "gen_ai.usage.output_tokens"), ("reasoning", "gen_ai.usage.reasoning_tokens"),
                                ("cache.read", "gen_ai.usage.cache_read.input_tokens"),
                                ("cache.write", "gen_ai.usage.cache_write.input_tokens")):
                                if source.startswith("cache."):
                                    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
                                    value = cache.get(source.split(".", 1)[1])
                                else:
                                    value = tokens.get(source)
                                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                                    model_attrs[target] = int(value)
                            cost = message_data.get("cost")
                            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost > 0:
                                model_attrs["gen_ai.usage.cost"] = float(cost)
                            model_id = identity("opencode-model", session_id, message_id, size=16)
                            accepted.append(normalize_native({"trace_id": trace_id, "span_id": model_id,
                                "parent_span_id": root_id, "name": "chat " + (model or "model call"),
                                "start_unix_nano": start_ns, "end_unix_nano": end_ns,
                                "status": "ERROR" if state == "error" else "OK", "attributes": model_attrs,
                                "resource_attributes": {"service.name": "opencode", "observatory.source_id": self.source_id},
                                "scope_name": "observatory.opencode"}, "opencode"))
                        if "part" not in tables or not {"id", "session_id", "message_id", "data"} <= part_columns:
                            continue
                        parts = db.execute(f'SELECT "id","data" FROM "part" WHERE "session_id"=? AND "message_id"=? ORDER BY "time_created" LIMIT ?',
                                           (session_id, message_id, MAX_PARTS_PER_MESSAGE)).fetchall()
                        for part in parts:
                            part_data = _json(part["data"])
                            if part_data.get("type") != "tool":
                                continue
                            tool_name = str(part_data.get("tool") or "tool")[:256]
                            tool_state = part_data.get("state") if isinstance(part_data.get("state"), dict) else {}
                            tool_status = str(tool_state.get("status") or "unknown").casefold()
                            tool_error = tool_status in {"error", "failed"}
                            tool_start = _first_timestamp((tool_state.get("time") or {}).get("start"), start_ns) or start_ns
                            tool_end = _first_timestamp((tool_state.get("time") or {}).get("end"), end_ns) or end_ns
                            mcp = mcp_attributes(tool_name) or _opencode_mcp_attributes(tool_name, self.mcp_servers)
                            tool_attrs = {"session.id": session_id, "gen_ai.operation.name": "execute_tool",
                                          "tool.name": tool_name, "tool.result_status": tool_status,
                                          "agent.activity.domain": "mcp" if mcp else "tool",
                                          "agent.activity.action": "call", "agent.activity.subject": tool_name,
                                          "agent.activity.result": "error" if tool_error else "success" if tool_status == "completed" else "unknown"}
                            tool_attrs.update(mcp)
                            metadata = part_data.get("metadata")
                            if isinstance(metadata, dict):
                                for key in ("mcp.server.name", "mcp.tool.name", "mcp.method.name"):
                                    if isinstance(metadata.get(key), str):
                                        tool_attrs[key] = metadata[key][:256]
                            if self.capture_details:
                                if "input" in tool_state:
                                    tool_attrs["input.value"] = safe(tool_state["input"], 4000)
                                if "output" in tool_state:
                                    tool_attrs["output.value"] = safe(tool_state["output"], 4000)
                            if tool_end < tool_start:
                                tool_start = tool_end
                            span_id = identity("opencode-tool", session_id, part["id"], size=16)
                            accepted.append(normalize_native({"trace_id": trace_id, "span_id": span_id,
                                "parent_span_id": model_id or root_id, "name": tool_name,
                                "start_unix_nano": tool_start, "end_unix_nano": tool_end,
                                "status": "ERROR" if tool_error else "OK" if tool_status == "completed" else "UNSET",
                                "attributes": tool_attrs,
                                "resource_attributes": {"service.name": "opencode", "observatory.source_id": self.source_id},
                                "scope_name": "observatory.opencode"}, "opencode"))
                    if latest_trace and latest_root_id:
                        progress = self._plan_progress(db, tables, session_id, latest_trace,
                                                       latest_root_id, updated)
                        if progress:
                            progress_logs.append(progress)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            self.errors += 1
            return 0
        self.sessions = sessions
        if accepted:
            self.store.ingest(accepted)
        if progress_logs:
            self.store.ingest_logs(progress_logs)
        self.fingerprint = fingerprint
        self.updated_ns = time.time_ns()
        return len(accepted)

