"""Read-only OpenCode session capture from its local SQLite database."""
from __future__ import annotations

import hashlib
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
from .native_json import loads

MAX_SESSIONS = 50
MAX_MESSAGES_PER_SESSION = 30
MAX_PARTS_PER_MESSAGE = 200
LOOKBACK_NS = 24 * 60 * 60 * 1_000_000_000
LOOKBACK_REFRESH_SECONDS = 30
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_POLL_PAYLOAD_BYTES = 16 * 1024 * 1024


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
        result = loads(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError, RecursionError):
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
        self.schema_errors = 0
        self.skipped_oversized = 0
        self.skipped_budget = 0
        self.budget_excluded_bytes = 0
        self.payload_bytes = 0
        self._last_refresh = 0
        self.mcp_servers = ()
        # Fixed-size digests, bounded to the current session/message window.
        # Never keep tool payloads or an unbounded history in memory.
        self._message_revisions = {}
        self._progress_revisions = {}
        self._part_revisions = {}
        self._payload_cursors = {}
        self._session_cursor = 0
        self.backlog_records = 0
        self._message_bytes = 0

    def diagnostics(self):
        return {"source_path": str(self.path), "last_success_ns": self.updated_ns,
                "schema_errors": self.schema_errors, "read_errors": self.errors,
                "skipped_oversized_records": self.skipped_oversized,
                "skipped_budget_records": self.skipped_budget,
                "budget_excluded_bytes_last_poll": self.budget_excluded_bytes,
                "backlog_records": self.backlog_records,
                "payload_bytes_last_poll": self.payload_bytes,
                "sessions_last_poll": len(self.sessions),
                "latest_session_update_ns": max((item.get("last_update_ns", 0)
                    for item in self.sessions), default=0),
                "max_payload_bytes": MAX_PAYLOAD_BYTES,
                "max_poll_payload_bytes": MAX_POLL_PAYLOAD_BYTES}

    def _payload_rows(self, db, table, fields, suffix, params, *, cursors, cursor_key,
                      messages=False, advance=True):
        """Inspect byte lengths before transferring any JSON into Python.

        Count caps alone cannot bound fetchall(): even one tool output may be
        enormous. Query only metadata first, then transfer eligible payloads.
        Oversized records are omitted. Budget-deferred rows resume first next
        time; rotating complete windows also prevents legacy schemas without
        timestamps from repeatedly consuming the budget on the same prefix.
        """
        selected = [f'"{name}"' for name in fields if name != "data"]
        selected.append('length(CAST("data" AS BLOB)) AS payload_size')
        rows = db.execute(f'SELECT {",".join(selected)} FROM "{table}"' + suffix, params).fetchall()
        order = {str(row["id"]): index for index, row in enumerate(rows)}
        start = order.get(cursors.get(cursor_key), 0)
        rows = rows[start:] + rows[:start]
        result = []
        deferred = None
        for row in rows:
            size = row["payload_size"] or 0
            if size > MAX_PAYLOAD_BYTES:
                self.skipped_oversized += 1
                continue
            # Reserve half the poll for tools, rather than preloading a whole
            # message window and leaving no budget for any of its parts.
            message_full = messages and self._message_bytes and self._message_bytes + size > MAX_POLL_PAYLOAD_BYTES // 2
            if self.payload_bytes + size > MAX_POLL_PAYLOAD_BYTES or message_full:
                self.skipped_budget += 1
                self.budget_excluded_bytes += size
                if size <= MAX_POLL_PAYLOAD_BYTES:
                    self.backlog_records += 1
                    deferred = deferred or str(row["id"])
                continue
            # The read transaction makes the size preflight and fetch coherent.
            raw = db.execute(f'SELECT "data" FROM "{table}" WHERE "id"=?', (row["id"],)).fetchone()[0]
            self.payload_bytes += size
            if messages:
                self._message_bytes += size
            data = dict(row)
            data.pop("payload_size")
            data["data"] = raw
            data["_window_order"] = order[str(row["id"])]
            result.append(data)
        if rows:
            cursors[cursor_key] = deferred or str(rows[(1 if advance else 0) % len(rows)]["id"])
        return result, set(order)

    def _decode(self, value):
        result = _json(value)
        if not result and value not in (None, "", "{}"):
            self.schema_errors += 1
        return result

    def set_mcp_servers(self, server_names):
        """Set OpenCode server names discovered from config, without settings."""
        servers = tuple(dict.fromkeys(str(name)[:128] for name in server_names if name))
        if servers != self.mcp_servers:
            self.mcp_servers = servers
            # Re-read persisted spans so newly discovered configuration can
            # enrich tool records that were initially ingested without it.
            self.fingerprint = None
            self._message_revisions.clear()
            self._part_revisions.clear()

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
        expressions = ['substr("content",1,500) AS "content"' if name == "content" else f'"{name}"' for name in selected]
        order = ' ORDER BY "position"' if "position" in columns else ""
        rows = db.execute(f'SELECT {",".join(expressions)} FROM "todo" WHERE "session_id"=?{order} LIMIT 100',
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
            self.fingerprint = None
            self._message_revisions.clear()
            self._progress_revisions.clear()
            self._part_revisions.clear()
            self._payload_cursors.clear()
            self.backlog_records = 0
            return 0
        if fingerprint == self.fingerprint and not self.backlog_records and time.monotonic() - self._last_refresh < LOOKBACK_REFRESH_SECONDS:
            return 0
        self.payload_bytes = 0
        self.budget_excluded_bytes = 0
        self.backlog_records = 0
        self._message_bytes = 0
        accepted = []
        progress_logs = []
        sessions = []
        message_revisions = dict(self._message_revisions)
        part_revisions = dict(self._part_revisions)
        cursors = dict(self._payload_cursors)
        message_window = set()
        part_windows = {}
        progress_revisions = {}
        try:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                db.execute("PRAGMA busy_timeout=250")
                db.execute("BEGIN")
                tables = self._tables(db)
                if "session" not in tables:
                    self.schema_errors += 1
                    self.sessions = []
                    self.discovered_sessions = 0
                    self.omitted_sessions = 0
                    self._message_revisions.clear()
                    self._progress_revisions.clear()
                    self._part_revisions.clear()
                    self._payload_cursors.clear()
                    self.fingerprint = fingerprint
                    self._last_refresh = time.monotonic()
                    return 0
                session_columns = self._columns(db, "session")
                required = {"id", "time_updated"}
                if not required <= session_columns:
                    self.schema_errors += 1
                    self.sessions = []
                    self.discovered_sessions = 0
                    self.omitted_sessions = 0
                    self._message_revisions.clear()
                    self._progress_revisions.clear()
                    self._part_revisions.clear()
                    self._payload_cursors.clear()
                    self.fingerprint = fingerprint
                    self._last_refresh = time.monotonic()
                    return 0
                columns = [name for name in ("id", "project_id", "title", "directory", "time_created",
                            "time_updated", "agent", "model", "tokens_input", "tokens_output")
                           if name in session_columns]
                now_ns = time.time_ns()
                cutoff_ms = (now_ns - LOOKBACK_NS) // 1_000_000
                message_table = "message" if "message" in tables else "session_message" if "session_message" in tables else None
                message_columns = self._columns(db, message_table) if message_table else set()
                part_columns = self._columns(db, "part") if "part" in tables else set()

                # Current OpenCode keeps appending to session_message while
                # retaining a stale session.time_updated (and old message rows)
                # after its storage migration. Discover sessions from recent
                # message activity as well as the session row timestamp.
                activity_updates = {}
                activity_tables = {}
                for table in ("message", "session_message"):
                    if table not in tables:
                        continue
                    cols = self._columns(db, table)
                    if "session_id" not in cols or not ({"time_updated", "time_created"} & cols):
                        continue
                    time_columns = [name for name in ("time_updated", "time_created") if name in cols]
                    stamp_expr = (f'COALESCE({",".join(chr(34)+name+chr(34) for name in time_columns)})'
                                  if len(time_columns) > 1 else f'"{time_columns[0]}"')
                    recent = db.execute(
                        f'SELECT "session_id",MAX({stamp_expr}) FROM "{table}" '
                        f'WHERE {stamp_expr}>=? GROUP BY "session_id"', (cutoff_ms,)).fetchall()
                    per_table = {str(row[0]): int(row[1] or 0) for row in recent}
                    activity_tables[table] = per_table
                    for session_id, stamp in per_table.items():
                        activity_updates[session_id] = max(activity_updates.get(session_id, 0), stamp)

                # Use SQL-side activity candidates so the result count remains
                # exact without loading every historic session or message.
                active_selects = []
                active_params = []
                for table, per_table in activity_tables.items():
                    cols = self._columns(db, table)
                    time_columns = [name for name in ("time_updated", "time_created") if name in cols]
                    stamp_expr = (f'COALESCE({",".join(chr(34)+name+chr(34) for name in time_columns)})'
                                  if len(time_columns) > 1 else f'"{time_columns[0]}"')
                    active_selects.append(
                        f'SELECT "session_id",MAX({stamp_expr}) AS activity_ns FROM "{table}" '
                        f'WHERE {stamp_expr}>=? GROUP BY "session_id"')
                    active_params.append(cutoff_ms)
                activity_cte = ('SELECT session_id,MAX(activity_ns) AS activity_ns FROM (' +
                                ' UNION ALL '.join(active_selects) + ') GROUP BY session_id'
                                if active_selects else 'SELECT NULL AS session_id,0 AS activity_ns WHERE 0')
                active_join = f'LEFT JOIN ({activity_cte}) recent ON recent.session_id=s.id'
                active_where = '(CAST(COALESCE(s.time_updated,0) AS INTEGER)>=? OR recent.session_id IS NOT NULL)'
                self.discovered_sessions = int(db.execute(
                    f'SELECT COUNT(*) FROM "session" s {active_join} WHERE {active_where}',
                    (*active_params, cutoff_ms)).fetchone()[0])
                self.omitted_sessions = max(0, self.discovered_sessions - MAX_SESSIONS)
                expressions = [f'substr(s."{name}",1,4096) AS "{name}"' if name in {"title", "directory", "agent", "model"} else f's."{name}"' for name in columns]
                expressions.append('MAX(CAST(COALESCE(s.time_updated,0) AS INTEGER),COALESCE(recent.activity_ns,0)) AS _last_activity_ns')
                sql = (f'SELECT {",".join(expressions)} FROM "session" s {active_join} '
                       f'WHERE {active_where} ORDER BY _last_activity_ns DESC,s.id LIMIT ?')
                rows = db.execute(sql, (*active_params, cutoff_ms, MAX_SESSIONS)).fetchall()
                # Newer OpenCode builds can retain recent session_message rows
                # after the corresponding session row has been removed (for
                # example, detached/subagent conversations). Preserve those
                # as metadata-only session entries rather than silently
                # dropping otherwise active work.
                activity_ids = set(activity_updates)
                existing_activity_ids = set()
                activity_id_list = sorted(activity_ids)
                for offset in range(0, len(activity_id_list), 500):
                    batch = activity_id_list[offset:offset + 500]
                    if not batch:
                        continue
                    marks = ",".join("?" for _ in batch)
                    existing_activity_ids.update(str(row[0]) for row in db.execute(
                        f'SELECT "id" FROM "session" WHERE "id" IN ({marks})', batch))
                orphan_ids = activity_ids - existing_activity_ids
                self.discovered_sessions += len(orphan_ids)
                self.omitted_sessions = max(0, self.discovered_sessions - MAX_SESSIONS)
                rows = list(rows)
                for session_id in sorted(orphan_ids, key=lambda key: activity_updates[key], reverse=True)[:MAX_SESSIONS]:
                    orphan = {name: None for name in columns}
                    orphan.update({"id": session_id, "time_updated": activity_updates[session_id],
                                   "title": "", "directory": "", "model": "", "_last_activity_ns": activity_updates[session_id]})
                    rows.append(orphan)
                rows.sort(key=lambda row: (-int((dict(row).get("_last_activity_ns") or 0)),
                                           str(dict(row).get("id") or "")))
                rows = rows[:MAX_SESSIONS]
                if rows:
                    start = self._session_cursor % len(rows)
                    rows = rows[start:] + rows[:start]
                project_names = {}
                if "project" in tables:
                    project_columns = self._columns(db, "project")
                    if "id" in project_columns:
                        project_fields = [name for name in ("id", "name", "worktree") if name in project_columns]
                        expressions = [f'"{name}"' if name == "id" else f'substr("{name}",1,4096) AS "{name}"' for name in project_fields]
                        project_ids = list(dict.fromkeys(row["project_id"] for row in rows if "project_id" in row.keys()))
                        placeholders = ",".join("?" for _ in project_ids)
                        for project_row in db.execute(f'SELECT {",".join(expressions)} FROM "project" WHERE "id" IN ({placeholders})', project_ids):
                            project = dict(project_row)
                            project_names[project["id"]] = project.get("name") or Path(project.get("worktree") or "").name
                if message_table and not {"id", "session_id", "data"} <= message_columns:
                    self.schema_errors += 1
                if "part" in tables and not {"id", "session_id", "message_id", "data"} <= part_columns:
                    self.schema_errors += 1
                for session in rows:
                    data = dict(session)
                    session_id = str(data["id"])
                    directory = str(data.get("directory") or "")
                    project = project_names.get(data.get("project_id")) or Path(directory).name or ""
                    updated = timestamp(data.get("_last_activity_ns", data.get("time_updated")))
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
                    # Prefer the message table that actually has the latest
                    # records for this session. Old databases may retain both
                    # tables after migration; existence alone is not enough.
                    table_candidates = [(updates.get(session_id, 0), name)
                                        for name, updates in activity_tables.items()]
                    read_table = max(table_candidates, default=(0, message_table))[1]
                    read_columns = self._columns(db, read_table) if read_table else set()
                    if not read_table or not {"id", "session_id", "data"} <= read_columns:
                        continue
                    msg_fields = [name for name in ("id", "session_id", "type", "time_created", "time_updated", "data") if name in read_columns]
                    order_by = ' ORDER BY "time_created" DESC' if "time_created" in read_columns else ""
                    messages, message_ids = self._payload_rows(db, read_table, msg_fields,
                        f' WHERE "session_id"=?{order_by} LIMIT ?', (session_id, MAX_MESSAGES_PER_SESSION),
                        cursors=cursors, cursor_key=(read_table, session_id, None), messages=True,
                        # Advance a complete message window on its session's
                        # priority poll, not on every visit: otherwise equal
                        # session/message counts can phase-lock and starve the
                        # same messages in legacy content-comparison schemas.
                        advance=session_id == str(rows[0]['id']))
                    if not message_ids and read_table == "message" and "session_message" in tables:
                        v2_columns = self._columns(db, "session_message")
                        if {"id", "session_id", "data"} <= v2_columns:
                            read_table = "session_message"
                            read_columns = v2_columns
                            msg_fields = [name for name in ("id", "session_id", "type", "time_created", "time_updated", "data") if name in v2_columns]
                            order_by = ' ORDER BY "time_created" DESC' if "time_created" in read_columns else ""
                            messages, message_ids = self._payload_rows(db, read_table, msg_fields,
                                f' WHERE "session_id"=?{order_by} LIMIT ?', (session_id, MAX_MESSAGES_PER_SESSION),
                                cursors=cursors, cursor_key=(read_table, session_id, None), messages=True,
                                advance=session_id == str(rows[0]['id']))
                    message_window.update((session_id, mid) for mid in message_ids)
                    latest_trace = latest_root_id = None
                    latest_order = float("inf")
                    for message in messages:
                        message_data = self._decode(message["data"])
                        role = message_data.get("role") or (message["type"] if "type" in message.keys() else "")
                        if str(role) != "assistant":
                            continue
                        message_id = str(message["id"])
                        trace_id = identity("opencode-trace", session_id, message_id)
                        root_id = identity("opencode-root", session_id, message_id, size=16)
                        if message["_window_order"] < latest_order:
                            latest_trace, latest_root_id = trace_id, root_id
                            latest_order = message["_window_order"]
                        # A write anywhere in OpenCode's DB used to re-import
                        # every recent message, including large tool outputs.
                        # Check cheap part revisions first. Older schemas without
                        # update timestamps fall back to content comparison.
                        parts = []
                        has_parts = "part" in tables and {"id", "session_id", "message_id", "data"} <= part_columns
                        part_order = ' ORDER BY "time_created","id"' if "time_created" in part_columns else ' ORDER BY "id"'
                        part_params = (session_id, message_id, MAX_PARTS_PER_MESSAGE)
                        part_query = ' FROM "part" WHERE "session_id"=? AND "message_id"=?' + part_order + ' LIMIT ?'
                        revision = hashlib.sha256(json.dumps(
                            (read_table, dict(message), data.get("title"), data.get("model"), project),
                            ensure_ascii=False, sort_keys=True).encode()).digest()
                        key = (session_id, message_id)
                        message_changed = self._message_revisions.get(key) != revision
                        message_revisions[key] = revision
                        part_stamps = {}
                        if has_parts:
                            field = '"time_updated"' if 'time_updated' in part_columns else 'NULL'
                            part_stamps = {str(row[0]): row[1] for row in db.execute(
                                'SELECT "id",' + field + part_query, part_params)}
                            part_windows[key] = set(part_stamps)
                            wanted = []
                            for pid, stamp in part_stamps.items():
                                version = hashlib.sha256(revision + str(stamp).encode()).digest() if stamp is not None else None
                                if version is None or part_revisions.get((*key, pid)) != version:
                                    wanted.append(pid)
                            if wanted:
                                placeholders = ','.join('?' for _ in wanted)
                                suffix = ' WHERE "session_id"=? AND "message_id"=? AND "id" IN (' + placeholders + ')' + part_order + ' LIMIT ?'
                                part_rows, _ = self._payload_rows(db, 'part', ['id', 'data'], suffix,
                                    (session_id, message_id, *wanted, MAX_PARTS_PER_MESSAGE),
                                    cursors=cursors, cursor_key=('part', session_id, message_id))
                                parts = [(row['id'], row['data']) for row in part_rows]
                        message_created = message["time_created"] if "time_created" in message.keys() else 0
                        message_updated = message["time_updated"] if "time_updated" in message.keys() else message_created
                        start_ns = _first_timestamp((message_data.get("time") or {}).get("created"), message_created)
                        end_ns = _first_timestamp((message_data.get("time") or {}).get("completed"),
                                                  (message_data.get("time") or {}).get("streamed"),
                                                  message_updated, message_created)
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
                        if message_changed:
                            accepted.append(root)
                        model = _model(message_data.get("modelID") or message_data.get("model") or data.get("model"))
                        tokens = message_data.get("tokens") if isinstance(message_data.get("tokens"), dict) else {}
                        model_id = None
                        if model or tokens:
                            model_data = message_data.get("model") if isinstance(message_data.get("model"), dict) else {}
                            model_attrs = {"session.id": session_id, "gen_ai.operation.name": "chat",
                                           "gen_ai.provider.name": str(message_data.get("providerID") or model_data.get("providerID") or "unknown"),
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
                                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 and (not isinstance(value, float) or math.isfinite(value)):
                                    model_attrs[target] = int(value)
                            cost = message_data.get("cost")
                            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost > 0:
                                model_attrs["gen_ai.usage.cost"] = float(cost)
                            model_id = identity("opencode-model", session_id, message_id, size=16)
                            model_span = normalize_native({"trace_id": trace_id, "span_id": model_id,
                                "parent_span_id": root_id, "name": "chat " + (model or "model call"),
                                "start_unix_nano": start_ns, "end_unix_nano": end_ns,
                                "status": "ERROR" if state == "error" else "OK", "attributes": model_attrs,
                                "resource_attributes": {"service.name": "opencode", "observatory.source_id": self.source_id},
                                "scope_name": "observatory.opencode"}, "opencode")
                            if message_changed:
                                accepted.append(model_span)
                        # New OpenCode session_message records carry tool
                        # blocks inline instead of referencing rows in part.
                        known_part_ids = {str(part_id) for part_id, _ in parts}
                        content = message_data.get("content")
                        if isinstance(content, list):
                            for index, block in enumerate(content[:MAX_PARTS_PER_MESSAGE]):
                                if not isinstance(block, dict) or block.get("type") != "tool":
                                    continue
                                inline_id = str(block.get("id") or f'{message_id}:tool:{index}')
                                if inline_id in known_part_ids:
                                    continue
                                inline_state = dict(block.get("state") or {})
                                if "output" not in inline_state and "content" in inline_state:
                                    inline_state["output"] = inline_state["content"]
                                inline = {"type": "tool", "tool": block.get("name") or "tool",
                                          "state": inline_state,
                                          "metadata": block.get("providerState") or {}}
                                parts.append((inline_id, json.dumps(inline, ensure_ascii=False)))
                                part_stamps[inline_id] = None
                        for part in parts:
                            part_id, raw_part = part
                            stamp = part_stamps[str(part_id)]
                            version = hashlib.sha256(revision + (str(stamp).encode() if stamp is not None else str(raw_part).encode())).digest()
                            part_key = (*key, str(part_id))
                            if part_revisions.get(part_key) == version:
                                continue
                            part_revisions[part_key] = version
                            part_data = self._decode(raw_part)
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
                            span_id = identity("opencode-tool", session_id, part_id, size=16)
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
                            revision = progress['attributes']['observatory.record_id']
                            progress_revisions[session_id] = revision
                            if self._progress_revisions.get(session_id) != revision:
                                progress_logs.append(progress)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError):
            self.errors += 1
            self.schema_errors += 1
            return 0
        self.sessions = sorted(sessions, key=lambda item: (-item['last_update_ns'], item['session_id']))
        if accepted:
            self.store.ingest(accepted)
        if progress_logs:
            self.store.ingest_logs(progress_logs)
        # Publish only after successful ingestion, so a failure can be retried.
        self._message_revisions = {key: value for key, value in message_revisions.items() if key in message_window}
        self._part_revisions = {key: value for key, value in part_revisions.items()
                                if key[:2] in message_window and
                                (key[:2] not in part_windows or key[2] in part_windows[key[:2]])}
        session_ids = {item['session_id'] for item in sessions}
        self._payload_cursors = {key: value for key, value in cursors.items()
                                 if key[1] in session_ids and (key[2] is None or key[1:] in message_window)}
        self._session_cursor += 1
        self._progress_revisions = progress_revisions
        self.fingerprint = fingerprint
        self.updated_ns = time.time_ns()
        self._last_refresh = time.monotonic()
        return len(accepted) + len(progress_logs)
