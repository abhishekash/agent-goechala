"""Normalize native JSONL and OTLP/JSON spans into one queryable store."""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sqlite3
import time
import base64
import threading
from pathlib import Path

from .findings import analyze

TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
SPAN_ID = re.compile(r"^[0-9a-fA-F]{16}$")
SESSION_KEYS = ("gen_ai.conversation.id", "session.id", "agent.session_id",
                "session_id", "conversation_id", "thread_id", "langgraph.thread_id",
                "langchain.session_id")
ENDPOINT_ID_KEYS = ("host.id", "host.name", "device.id", "service.instance.id")
ENDPOINT_RESOURCE_KEYS = frozenset((*ENDPOINT_ID_KEYS, "host.arch", "os.type", "os.name",
                                    "os.description", "os.version", "user.name", "user.id",
                                    "deployment.environment.name", "cloud.region"))


def _id(value, pattern, label):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}")
    if not any(c != "0" for c in value):
        raise ValueError(f"invalid zero {label}")
    return value.lower()


def _session_value(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= 512 else None


def _endpoint_identity(resource):
    if not isinstance(resource, dict):
        return None
    for key in ENDPOINT_ID_KEYS:
        value = resource.get(key)
        if isinstance(value, str) and value.strip():
            return key, value.strip()[:512]
    return None


def _session_for(spans, progress, logs):
    ordered_spans = sorted(spans, key=lambda span: (bool(span["parent_span_id"]), span["start_unix_nano"]))
    for key in SESSION_KEYS:
        for span in ordered_spans:
            value = _session_value(span["attributes"].get(key) or span["resource_attributes"].get(key))
            if value:
                return value
            for event in span["events"]:
                value = _session_value(event.get("attributes", {}).get(key))
                if value:
                    return value
        for log in logs:
            value = _session_value(log["attributes"].get(key) or log["resource_attributes"].get(key))
            if value:
                return value
        if key == "session.id":
            for event in progress:
                value = _session_value(event.get("session_id"))
                if value:
                    return value
    return None


def _activity_row(attrs, time_ns, source, source_id, span_id=None, service="unknown", body=None):
    """Normalize the shared agent.activity event vocabulary for the trace ledger."""
    attrs = attrs if isinstance(attrs, dict) else {}
    def value(*keys):
        for key in keys:
            item = attrs.get(key)
            if item is not None and item != "":
                return str(item)
        return ""
    try:
        time_ns = int(time_ns or 0)
    except (TypeError, ValueError, OverflowError):
        time_ns = 0
    return {"time_unix_nano": max(0, time_ns),
            "domain": value("agent.activity.domain", "activity.domain") or "other",
            "action": value("agent.activity.action", "activity.action") or "activity",
            "subject": value("agent.activity.subject", "agent.activity.target",
                             "activity.subject", "activity.target"),
            "result": value("agent.activity.result", "activity.result") or "unknown",
            "actor": value("agent.activity.actor", "agent.actor"),
            "service": str(service), "source": source, "source_id": str(source_id),
            "span_id": span_id, "detail": body if isinstance(body, str) else "",
            "mcp_server": value("mcp.server.name"), "mcp_tool": value("mcp.tool.name"),
            "duration_ms": attrs.get("duration_ms")}


def _progress_from_log(log):
    attrs = log.get("attributes") if isinstance(log.get("attributes"), dict) else {}
    event_name = log.get("event_name") or attrs.get("event.name")
    if event_name not in {"agent.progress", "progress"}:
        return None
    body = log.get("body")
    body_map = body if isinstance(body, dict) else {}
    def first(*values):
        return next((value for value in values if value is not None and value != ""), None)
    percent = first(attrs.get("agent.progress.percent"), attrs.get("progress.percent"),
                    body_map.get("percent"))
    try:
        percent = None if isinstance(percent, bool) else (float(percent) if percent is not None else None)
        if percent is not None and (not math.isfinite(percent) or not 0 <= percent <= 100):
            percent = None
    except (TypeError, ValueError, OverflowError):
        percent = None
    if isinstance(body, str):
        body_message = body
    elif body_map:
        body_message = first(body_map.get("message"), body_map.get("detail"), "")
    else:
        body_message = ""
    session_id = first(*(attrs.get(key) for key in SESSION_KEYS),
                       *(log.get("resource_attributes", {}).get(key) for key in SESSION_KEYS))
    return {"time_ns": log.get("time_unix_nano", 0),
            "stage": str(first(attrs.get("agent.progress.stage"), attrs.get("progress.stage"),
                               body_map.get("stage"), "progress")),
            "message": str(first(attrs.get("agent.progress.message"), attrs.get("progress.message"),
                                 body_message, "")),
            "percent": percent, "session_id": _session_value(session_id),
            "service": log.get("service")}


def _tool_activity(span):
    attrs = span.get("attributes") if isinstance(span.get("attributes"), dict) else {}
    operation = str(attrs.get("gen_ai.operation.name") or "").lower()
    openinference_tool = attrs.get("openinference.span.kind") == "TOOL"
    tool_name = (attrs.get("gen_ai.tool.name") or attrs.get("mcp.tool.name") or
                 attrs.get("tool.name"))
    if not tool_name and operation != "execute_tool" and not openinference_tool:
        return None
    tool_name = str(tool_name or span.get("name") or "tool call")
    domain = (attrs.get("gen_ai.tool.type") or attrs.get("tool.type") or
              ("mcp" if attrs.get("mcp.server.name") or attrs.get("mcp.method.name") else
               "openinference" if openinference_tool else "tool"))
    action = (attrs.get("tool.action") or attrs.get("mcp.method.name") or
              operation or "call")
    status = str(attrs.get("tool.result_status") or "").lower()
    if span.get("status") == "ERROR" or attrs.get("error.type") or attrs.get("tool.error"):
        result = "error"
    elif status in {"error", "failed", "failure"}:
        result = "error"
    elif status in {"success", "ok", "completed"} or span.get("status") == "OK":
        result = "success"
    else:
        result = "unknown"
    normalized = {
        "agent.activity.domain": str(domain),
        "agent.activity.action": str(action),
        "agent.activity.subject": tool_name,
        "agent.activity.result": result,
        "agent.activity.actor": str(attrs.get("gen_ai.agent.name") or attrs.get("agent.name") or ""),
    }
    return _activity_row(normalized, span.get("end_unix_nano") or span.get("start_unix_nano"),
                         "span", span["span_id"], span["span_id"], span.get("service", "unknown"))


def _activity_for(spans, logs):
    activity = []
    explicit_span_ids = set()
    for span in spans:
        for event in span["events"]:
            if not isinstance(event, dict) or event.get("name") != "agent.activity":
                continue
            explicit_span_ids.add(span["span_id"])
            activity.append(_activity_row(event.get("attributes"),
                                          event.get("time_unix_nano") or span["start_unix_nano"],
                                          "span", span["span_id"], span["span_id"], span["service"]))
    linked_logs = {log.get("span_id") for log in logs if log["event_name"] == "agent.activity"}
    for span in spans:
        if span["span_id"] not in explicit_span_ids and span["span_id"] not in linked_logs:
            inferred = _tool_activity(span)
            if inferred is not None:
                activity.append(inferred)
    for log in logs:
        if log["event_name"] != "agent.activity":
            continue
        activity.append(_activity_row(log["attributes"], log["time_unix_nano"], "log",
                                      log["id"], log.get("span_id"), log["service"],
                                      log["body"] if isinstance(log["body"], str) else ""))
    return sorted(activity, key=lambda item: (item["time_unix_nano"], item["source_id"]))


def _attrs(items):
    result = {}
    for item in items or []:
        result[item["key"]] = _value(item.get("value", {}))
    return result


def _value(value):
    if "arrayValue" in value:
        return [_value(x) for x in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return _attrs(value["kvlistValue"].get("values", []))
    for key in ("stringValue", "intValue", "doubleValue", "boolValue", "bytesValue"):
        if key in value:
            v = value[key]
            return int(v) if key == "intValue" else v
    return None


def normalize_native(raw, service="agent-harness"):
    trace_id = _id(raw.get("trace_id"), TRACE_ID, "trace_id")
    span_id = _id(raw.get("span_id"), SPAN_ID, "span_id")
    parent = raw.get("parent_span_id")
    if parent:
        parent = _id(parent, SPAN_ID, "parent_span_id")
    start, end = int(raw["start_unix_nano"]), int(raw["end_unix_nano"])
    if start < 0 or end < start:
        raise ValueError("invalid span timestamps")
    attrs = raw.get("attributes") or {}
    events = raw.get("events") or []
    links = raw.get("links") or []
    resource = raw.get("resource_attributes") or {}
    if not isinstance(attrs, dict) or not isinstance(events, list) or not isinstance(links, list) or not isinstance(resource, dict):
        raise ValueError("attributes/resource must be objects and events/links arrays")
    normalized_links = []
    for link in links:
        normalized_links.append({"trace_id": _id(link.get("trace_id"), TRACE_ID, "link trace_id"),
                                 "span_id": _id(link.get("span_id"), SPAN_ID, "link span_id"),
                                 "attributes": link.get("attributes") or {}})
    return {"trace_id": trace_id, "span_id": span_id, "parent_span_id": parent,
            "name": str(raw["name"]), "start_unix_nano": start,
            "end_unix_nano": end, "status": str(raw.get("status", "UNSET")),
            "service": service, "attributes": attrs, "events": events,
            "links": normalized_links, "resource_attributes": resource,
            "scope_name": str(raw.get("scope_name") or "")}


def normalize_otlp(body):
    spans = []
    for group in body.get("resourceSpans", []):
        resource = _attrs(group.get("resource", {}).get("attributes"))
        service = str(resource.get("service.name") or "unknown")
        for scope in group.get("scopeSpans", []):
            for raw in scope.get("spans", []):
                status_code = int(raw.get("status", {}).get("code", 0))
                native = {
                    "trace_id": raw.get("traceId"), "span_id": raw.get("spanId"),
                    "parent_span_id": raw.get("parentSpanId") or None,
                    "name": raw.get("name", "unnamed"),
                    "start_unix_nano": raw.get("startTimeUnixNano"),
                    "end_unix_nano": raw.get("endTimeUnixNano"),
                    "status": {0: "UNSET", 1: "OK", 2: "ERROR"}.get(status_code, "UNSET"),
                    "attributes": _attrs(raw.get("attributes")),
                    "resource_attributes": resource,
                    "scope_name": scope.get("scope", {}).get("name", ""),
                    "events": [{"name": e.get("name", "event"),
                                "time_unix_nano": int(e.get("timeUnixNano", 0)),
                                "attributes": _attrs(e.get("attributes"))}
                               for e in raw.get("events", [])],
                    "links": [{"trace_id": link.get("traceId"),
                               "span_id": link.get("spanId"),
                               "attributes": _attrs(link.get("attributes"))}
                              for link in raw.get("links", [])],
                }
                spans.append(normalize_native(native, service))
    return spans


class Store:
    def __init__(self, path, async_derived_refresh=False):
        self.path = str(path)
        self._async_derived_refresh = bool(async_derived_refresh)
        self._derived_condition = threading.Condition()
        self._derived_pending = {}
        self._derived_stopping = False
        self._derived_thread = None
        self.last_derived_error = None
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS spans (
                    trace_id TEXT NOT NULL, span_id TEXT NOT NULL,
                    parent_span_id TEXT, name TEXT NOT NULL,
                    start_ns INTEGER NOT NULL, end_ns INTEGER NOT NULL,
                    status TEXT NOT NULL, service TEXT NOT NULL,
                    attributes TEXT NOT NULL, events TEXT NOT NULL,
                    links TEXT NOT NULL DEFAULT '[]',
                    resource_attributes TEXT NOT NULL DEFAULT '{}',
                    scope_name TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (trace_id, span_id));
                CREATE INDEX IF NOT EXISTS spans_time ON spans(end_ns DESC);
                CREATE INDEX IF NOT EXISTS spans_service_time ON spans(service,end_ns);
                CREATE TABLE IF NOT EXISTS agent_connections (
                    source_id TEXT PRIMARY KEY, service TEXT NOT NULL, runtime TEXT NOT NULL,
                    integrations TEXT NOT NULL, capture_content INTEGER NOT NULL,
                    process_id INTEGER, started_ns INTEGER NOT NULL, last_seen_ns INTEGER NOT NULL,
                    state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS progress (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL,
                    time_ns INTEGER NOT NULL, stage TEXT NOT NULL,
                    message TEXT NOT NULL, percent REAL, session_id TEXT, service TEXT);
                CREATE INDEX IF NOT EXISTS progress_trace ON progress(trace_id, time_ns);
                CREATE TABLE IF NOT EXISTS logs (
                    digest TEXT PRIMARY KEY, trace_id TEXT, span_id TEXT,
                    time_ns INTEGER NOT NULL, service TEXT NOT NULL,
                    severity_number INTEGER NOT NULL, severity_text TEXT NOT NULL,
                    event_name TEXT NOT NULL, body TEXT NOT NULL,
                    attributes TEXT NOT NULL, resource_attributes TEXT NOT NULL,
                    scope_name TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS logs_trace ON logs(trace_id,time_ns);
                CREATE INDEX IF NOT EXISTS logs_time ON logs(time_ns DESC);
                CREATE INDEX IF NOT EXISTS logs_native_record ON logs(json_extract(attributes,'$."observatory.record_id"'))
                    WHERE scope_name='observatory.local_agents';
                CREATE TABLE IF NOT EXISTS metric_points (
                    digest TEXT PRIMARY KEY, time_ns INTEGER NOT NULL,
                    start_ns INTEGER NOT NULL, service TEXT NOT NULL,
                    name TEXT NOT NULL, description TEXT NOT NULL,
                    unit TEXT NOT NULL, kind TEXT NOT NULL,
                    temporality INTEGER NOT NULL, attributes TEXT NOT NULL,
                    resource_attributes TEXT NOT NULL, scope_name TEXT NOT NULL,
                    data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS metric_points_time ON metric_points(time_ns DESC);
                CREATE TABLE IF NOT EXISTS outcomes (
                    trace_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    passed INTEGER NOT NULL, scores TEXT NOT NULL,
                    error TEXT NOT NULL, provider_model TEXT NOT NULL,
                    source TEXT NOT NULL, details TEXT NOT NULL,
                    ingested_ns INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS trace_activity (
                    trace_id TEXT PRIMARY KEY, last_ns INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS trace_activity_time ON trace_activity(last_ns DESC,trace_id DESC);
                CREATE TABLE IF NOT EXISTS endpoints (
                    endpoint_id TEXT PRIMARY KEY, identity_key TEXT NOT NULL,
                    identity_value TEXT NOT NULL, resource_attributes TEXT NOT NULL,
                    last_ns INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS endpoint_services (
                    endpoint_id TEXT NOT NULL, service TEXT NOT NULL, last_ns INTEGER NOT NULL,
                    PRIMARY KEY(endpoint_id,service));
                CREATE TABLE IF NOT EXISTS endpoint_traces (
                    endpoint_id TEXT NOT NULL, trace_id TEXT NOT NULL, last_ns INTEGER NOT NULL,
                    PRIMARY KEY(endpoint_id,trace_id));
                CREATE INDEX IF NOT EXISTS endpoint_traces_recent ON endpoint_traces(endpoint_id,last_ns DESC);
                CREATE TABLE IF NOT EXISTS trace_summaries (
                    trace_id TEXT PRIMARY KEY, session_id TEXT, summary_json TEXT NOT NULL,
                    activity_json TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS finding_cache (
                    finding_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL,
                    time_ns INTEGER NOT NULL, finding_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS finding_cache_trace ON finding_cache(trace_id,time_ns DESC);
                CREATE INDEX IF NOT EXISTS finding_cache_time ON finding_cache(time_ns DESC,finding_id DESC);
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(spans)")}
            for name, declaration in (("links", "TEXT NOT NULL DEFAULT '[]'"),
                                      ("resource_attributes", "TEXT NOT NULL DEFAULT '{}'"),
                                      ("scope_name", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    db.execute(f"ALTER TABLE spans ADD COLUMN {name} {declaration}")
            for table, name in (("progress", "session_id"), ("progress", "service"), ("trace_summaries", "session_id")):
                table_columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if name not in table_columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")
            summary_columns = {row[1] for row in db.execute("PRAGMA table_info(trace_summaries)")}
            if "activity_json" not in summary_columns:
                db.execute("ALTER TABLE trace_summaries ADD COLUMN activity_json TEXT NOT NULL DEFAULT '[]'")
            db.execute("CREATE INDEX IF NOT EXISTS trace_summaries_session ON trace_summaries(session_id,trace_id)")
            if not db.execute("SELECT 1 FROM schema_meta WHERE key='trace_activity_v1'").fetchone():
                db.execute("""INSERT INTO trace_activity(trace_id,last_ns)
                    SELECT trace_id,MAX(activity_ns) FROM (
                        SELECT trace_id,end_ns AS activity_ns FROM spans
                        UNION ALL SELECT trace_id,time_ns FROM progress
                        UNION ALL SELECT trace_id,time_ns FROM logs WHERE trace_id IS NOT NULL
                        UNION ALL SELECT trace_id,ingested_ns FROM outcomes
                    ) GROUP BY trace_id""")
                db.execute("INSERT INTO schema_meta(key,value) VALUES('trace_activity_v1','1')")
        os.chmod(self.path, 0o600)
        self._backfill_summaries()
        self._backfill_endpoints()
        if self._async_derived_refresh:
            self._derived_thread = threading.Thread(target=self._derived_refresh_worker,
                                                   name="observatory-derived-refresh", daemon=True)
            self._derived_thread.start()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def register_connection(self, record):
        source_id = _id(record.get("source_id"), TRACE_ID, "source_id")
        service = record.get("service")
        integrations = record.get("integrations")
        runtime = record.get("runtime")
        state = record.get("state", "running")
        capture_content = record.get("capture_content", False)
        process_id = record.get("process_id")
        if not isinstance(service, str) or not 1 <= len(service.strip()) <= 128:
            raise ValueError("service must contain 1 to 128 characters")
        if not isinstance(runtime, str) or not 1 <= len(runtime) <= 80:
            raise ValueError("invalid runtime")
        if (not isinstance(integrations, list) or not 1 <= len(integrations) <= 20 or
                any(not isinstance(item, str) or not 1 <= len(item) <= 80 for item in integrations)):
            raise ValueError("invalid integrations")
        if state not in {"running", "stopped"} or not isinstance(capture_content, bool):
            raise ValueError("invalid connection state")
        if process_id is not None and (isinstance(process_id, bool) or not isinstance(process_id, int) or not 0 < process_id < 2**31):
            raise ValueError("invalid process_id")
        now = time.time_ns()
        with self._connect() as db:
            db.execute("""INSERT INTO agent_connections VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET last_seen_ns=excluded.last_seen_ns,
                state=excluded.state,service=excluded.service,runtime=excluded.runtime,integrations=excluded.integrations,
                capture_content=excluded.capture_content,process_id=excluded.process_id""", (source_id, service.strip(), runtime,
                json.dumps(integrations), int(capture_content), process_id, now, now, state))

    def redact_internal_source(self, source_id):
        """Keep internal approval reviews as metadata, including older captures."""
        source_id = _id(source_id, TRACE_ID, "source_id")
        affected = set()
        with self._connect() as db:
            rows = db.execute("SELECT digest,trace_id,attributes FROM logs WHERE instr(resource_attributes,?)>0", (source_id,)).fetchall()
            for row in rows:
                attrs = json.loads(row["attributes"])
                attrs["agent.activity.subject"] = "Approval review"
                db.execute("UPDATE logs SET body=?,attributes=?,service='codex-review' WHERE digest=?",
                           (json.dumps(""), json.dumps(attrs), row["digest"]))
                if row["trace_id"]:
                    affected.add(row["trace_id"])
            rows = db.execute("SELECT trace_id,span_id,attributes,name FROM spans WHERE instr(resource_attributes,?)>0", (source_id,)).fetchall()
            for row in rows:
                attrs = json.loads(row["attributes"])
                if row["name"] == "agent.run":
                    attrs["agent.task"] = "Approval review"
                db.execute("UPDATE spans SET attributes=?,service='codex-review' WHERE trace_id=? AND span_id=?",
                           (json.dumps(attrs), row["trace_id"], row["span_id"]))
                affected.add(row["trace_id"])
        if affected:
            self._refresh_after_write(affected)

    def connections(self):
        now = time.time_ns()
        with self._connect() as db:
            rows = db.execute("""SELECT c.*,
                (SELECT COUNT(*) FROM spans s WHERE s.service=c.service
                 AND instr(s.resource_attributes,c.source_id)>0) AS span_count
                FROM agent_connections c ORDER BY c.last_seen_ns DESC LIMIT 50""").fetchall()
        return [{"source_id": r["source_id"], "service": r["service"], "runtime": r["runtime"],
                 "integrations": json.loads(r["integrations"]), "capture_content": bool(r["capture_content"]),
                 "process_id": r["process_id"], "started_ns": r["started_ns"], "last_seen_ns": r["last_seen_ns"],
                 "state": "offline" if r["state"] == "running" and now-r["last_seen_ns"]>35_000_000_000 else r["state"],
                 "span_count": r["span_count"]} for r in rows]

    def _backfill_summaries(self):
        with self._connect() as db:
            complete = db.execute("SELECT 1 FROM schema_meta WHERE key='trace_derived_v9'").fetchone()
        if not complete:
            before = None
            while True:
                sql = "SELECT trace_id,last_ns FROM trace_activity"
                params = []
                if before:
                    sql += " WHERE last_ns < ? OR (last_ns = ? AND trace_id < ?)"
                    params.extend((before[0], before[0], before[1]))
                sql += " ORDER BY last_ns DESC,trace_id DESC LIMIT 100"
                with self._connect() as db:
                    rows = db.execute(sql, params).fetchall()
                if not rows:
                    break
                self._refresh_summaries(row["trace_id"] for row in rows)
                before = (rows[-1]["last_ns"], rows[-1]["trace_id"])
                if len(rows) < 100:
                    break
        if not complete:
            with self._connect() as db:
                db.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('trace_derived_v9','1')")

    def _backfill_endpoints(self):
        with self._connect() as db:
            complete = db.execute("SELECT 1 FROM schema_meta WHERE key='endpoint_inventory_v1'").fetchone()
        if complete:
            return
        queries = (
            "SELECT trace_id,service,resource_attributes,MAX(end_ns) AS time_unix_nano "
            "FROM spans GROUP BY trace_id,service,resource_attributes",
            "SELECT trace_id,service,resource_attributes,MAX(time_ns) AS time_unix_nano "
            "FROM logs GROUP BY trace_id,service,resource_attributes",
            "SELECT NULL AS trace_id,service,resource_attributes,MAX(time_ns) AS time_unix_nano "
            "FROM metric_points GROUP BY service,resource_attributes",
        )
        for query in queries:
            with self._connect() as db:
                cursor = db.execute(query)
                while True:
                    rows = cursor.fetchmany(500)
                    if not rows:
                        break
                    records = [{"trace_id": row["trace_id"], "service": row["service"],
                                "resource_attributes": json.loads(row["resource_attributes"]),
                                "time_unix_nano": row["time_unix_nano"]}
                               for row in rows]
                    self._update_endpoints(db, records)
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('endpoint_inventory_v1','1')")

    def _refresh_summaries(self, trace_ids):
        updates = []
        findings = []
        replaced = []
        for trace_id in set(trace_ids):
            trace = self.trace(trace_id)
            summary = trace["summary"]
            if summary is None:
                continue
            session_id = summary.get("session_id")
            updates.append((trace_id, session_id, json.dumps(summary, separators=(",", ":")),
                            json.dumps(trace["activity"], separators=(",", ":"))))
            replaced.append((trace_id,))
            evidence_logs = self._finding_logs(trace_id)
            timestamp = max([span["end_unix_nano"] for span in trace["spans"]] +
                            [log["time_unix_nano"] for log in evidence_logs] +
                            ([trace["outcome"]["ingested_ns"]] if trace["outcome"] else []),
                            default=summary["end_unix_nano"])
            findings.extend((item["id"], trace_id, timestamp,
                             json.dumps({**item, "time_unix_nano": timestamp}, separators=(",", ":")))
                            for item in analyze(trace["spans"], evidence_logs, trace["outcome"]))
        if updates or replaced:
            with self._connect() as db:
                if updates:
                    db.executemany("INSERT INTO trace_summaries(trace_id,session_id,summary_json,activity_json) VALUES(?,?,?,?) "
                                   "ON CONFLICT(trace_id) DO UPDATE SET session_id=excluded.session_id,"
                                   "summary_json=excluded.summary_json,activity_json=excluded.activity_json",
                                   updates)
                if replaced:
                    db.executemany("DELETE FROM finding_cache WHERE trace_id=?", replaced)
                    db.executemany("INSERT INTO finding_cache(finding_id,trace_id,time_ns,finding_json) "
                                   "VALUES(?,?,?,?)", findings)

    def _refresh_after_write(self, trace_ids):
        trace_ids = set(trace_ids)
        if not trace_ids:
            return
        if not self._async_derived_refresh or self._derived_thread is None:
            self._refresh_summaries(trace_ids)
            return
        now = time.monotonic()
        with self._derived_condition:
            for trace_id in trace_ids:
                pending = self._derived_pending.get(trace_id)
                self._derived_pending[trace_id] = (pending[0] if pending else now, now,
                                                   pending[2] if pending else 0)
            self._derived_condition.notify()

    def _derived_refresh_worker(self):
        quiet_period = 0.25
        max_delay = 1.0
        while True:
            with self._derived_condition:
                while not self._derived_pending and not self._derived_stopping:
                    self._derived_condition.wait()
                if self._derived_stopping and not self._derived_pending:
                    return
                now = time.monotonic()
                ready = [trace_id for trace_id, (first, last, _) in self._derived_pending.items()
                         if self._derived_stopping or now - last >= quiet_period or now - first >= max_delay]
                if not ready:
                    wait_for = min(min(quiet_period - (now - last), max_delay - (now - first))
                                   for first, last, _ in self._derived_pending.values())
                    self._derived_condition.wait(timeout=max(0.01, wait_for))
                    continue
                pending = {trace_id: self._derived_pending.pop(trace_id) for trace_id in ready}
            try:
                self._refresh_summaries(ready)
                self.last_derived_error = None
            except Exception as exc:
                self.last_derived_error = str(exc)
                if not self._derived_stopping:
                    now = time.monotonic()
                    with self._derived_condition:
                        for trace_id, (first, _, attempts) in pending.items():
                            if attempts < 4 and trace_id not in self._derived_pending:
                                self._derived_pending[trace_id] = (first, now, attempts + 1)
                        self._derived_condition.notify()

    def close(self):
        if self._derived_thread is None:
            return
        with self._derived_condition:
            self._derived_stopping = True
            self._derived_condition.notify_all()
        self._derived_thread.join()

    def _finding_logs(self, trace_id):
        with self._connect() as db:
            rows = db.execute("SELECT digest,trace_id,time_ns,severity_number,body,event_name,attributes FROM logs "
                              "WHERE trace_id=? "
                              "ORDER BY time_ns",
                              (trace_id,)).fetchall()
        return [{"id": row["digest"], "trace_id": row["trace_id"],
                 "time_unix_nano": row["time_ns"], "severity_number": row["severity_number"],
                 "body": json.loads(row["body"]), "event_name": row["event_name"],
                 "attributes": json.loads(row["attributes"])} for row in rows]

    @staticmethod
    def _update_endpoints(db, records):
        endpoints = {}
        services = {}
        traces = {}
        for record in records:
            resource = record.get("resource_attributes") or {}
            identity = _endpoint_identity(resource)
            if identity is None:
                continue
            identity_key, identity_value = identity
            endpoint_id = hashlib.sha256(f"{identity_key}\0{identity_value}".encode()).hexdigest()[:32]
            timestamp = int(record.get("end_unix_nano") or record.get("time_unix_nano") or
                            record.get("start_unix_nano") or time.time_ns())
            attributes = {key: value for key, value in resource.items()
                          if key in ENDPOINT_RESOURCE_KEYS and isinstance(value, (str, int, float, bool))}
            current = endpoints.get(endpoint_id)
            if current is None or timestamp >= current[3]:
                endpoints[endpoint_id] = (endpoint_id, identity_key, identity_value,
                                          timestamp, json.dumps(attributes, separators=(",", ":")))
            service = str(record.get("service") or "unknown")
            services[(endpoint_id, service)] = max(timestamp, services.get((endpoint_id, service), 0))
            trace_id = record.get("trace_id")
            if trace_id:
                traces[(endpoint_id, str(trace_id))] = max(timestamp, traces.get((endpoint_id, str(trace_id)), 0))
        if endpoints:
            db.executemany("""INSERT INTO endpoints(endpoint_id,identity_key,identity_value,last_ns,resource_attributes)
                VALUES(?,?,?,?,?) ON CONFLICT(endpoint_id) DO UPDATE SET
                last_ns=MAX(last_ns,excluded.last_ns),
                resource_attributes=CASE WHEN excluded.last_ns>=last_ns
                    THEN excluded.resource_attributes ELSE resource_attributes END""",
                [(key, identity_key, identity_value, last_ns, attrs)
                 for key, identity_key, identity_value, last_ns, attrs in endpoints.values()])
            db.executemany("""INSERT INTO endpoint_services(endpoint_id,service,last_ns) VALUES(?,?,?)
                ON CONFLICT(endpoint_id,service) DO UPDATE SET last_ns=MAX(last_ns,excluded.last_ns)""",
                [(endpoint_id, service, last_ns)
                 for (endpoint_id, service), last_ns in services.items()])
            db.executemany("""INSERT INTO endpoint_traces(endpoint_id,trace_id,last_ns) VALUES(?,?,?)
                ON CONFLICT(endpoint_id,trace_id) DO UPDATE SET last_ns=MAX(last_ns,excluded.last_ns)""",
                [(endpoint_id, trace_id, last_ns)
                 for (endpoint_id, trace_id), last_ns in traces.items()])

    def endpoints(self, limit=100):
        if not 1 <= int(limit) <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as db:
            rows = db.execute("""SELECT e.*,
                (SELECT COUNT(*) FROM endpoint_traces t WHERE t.endpoint_id=e.endpoint_id) AS trace_count,
                (SELECT trace_id FROM endpoint_traces t WHERE t.endpoint_id=e.endpoint_id
                    ORDER BY last_ns DESC,trace_id DESC LIMIT 1) AS latest_trace_id
                FROM endpoints e ORDER BY e.last_ns DESC,e.endpoint_id LIMIT ?""", (int(limit),)).fetchall()
            endpoint_ids = [row["endpoint_id"] for row in rows]
            services = (db.execute("SELECT endpoint_id,service FROM endpoint_services WHERE endpoint_id IN (" +
                                   ",".join("?" for _ in endpoint_ids) + ") ORDER BY service", endpoint_ids).fetchall()
                        if endpoint_ids else [])
        service_map = {}
        for row in services:
            service_map.setdefault(row["endpoint_id"], []).append(row["service"])
        result = []
        for row in rows:
            attrs = json.loads(row["resource_attributes"])
            result.append({"endpoint_id": row["endpoint_id"],
                           "name": attrs.get("host.name") or attrs.get("device.id") or row["identity_value"],
                           "identity_key": row["identity_key"],
                           "identity_value": row["identity_value"],
                           "os_type": attrs.get("os.type") or attrs.get("os.name") or "",
                           "os_version": attrs.get("os.version") or "",
                           "architecture": attrs.get("host.arch") or "",
                           "user": attrs.get("user.name") or attrs.get("user.id") or "",
                           "environment": attrs.get("deployment.environment.name") or "",
                           "cloud_region": attrs.get("cloud.region") or "",
                           "services": service_map.get(row["endpoint_id"], []),
                           "trace_count": row["trace_count"],
                           "latest_trace_id": row["latest_trace_id"],
                           "last_seen_unix_nano": row["last_ns"]})
        return result

    def usage_series(self, days=7, step="day", service=None):
        days = int(days)
        if not 1 <= days <= 90:
            raise ValueError("days must be between 1 and 90")
        if step not in {"hour", "day"}:
            raise ValueError("step must be hour or day")
        step_ns = 3_600_000_000_000 if step == "hour" else 86_400_000_000_000
        now_ns = time.time_ns()
        cutoff_ns = now_ns - days * 86_400_000_000_000
        first_bucket = cutoff_ns // step_ns * step_ns
        last_bucket = now_ns // step_ns * step_ns
        buckets = {bucket: {"start_unix_nano": bucket, "runs": 0, "errors": 0,
                            "input_tokens": 0, "output_tokens": 0,
                            "known_cost_usd": 0.0, "unknown_cost_runs": 0}
                   for bucket in range(first_bucket, last_bucket + 1, step_ns)}
        with self._connect() as db:
            rows = db.execute("""SELECT s.summary_json FROM trace_summaries s
                JOIN trace_activity a USING(trace_id) WHERE a.last_ns>=?""", (cutoff_ns,)).fetchall()
        for row in rows:
            summary = json.loads(row["summary_json"])
            if service and service not in summary.get("services", []):
                continue
            end_ns = int(summary.get("end_unix_nano") or 0)
            if end_ns < cutoff_ns or end_ns > now_ns:
                continue
            bucket = end_ns // step_ns * step_ns
            item = buckets.get(bucket)
            if item is None:
                continue
            item["runs"] += 1
            item["errors"] += summary.get("status") in {"ERROR", "FAILED"}
            item["input_tokens"] += int(summary.get("input_tokens") or 0)
            item["output_tokens"] += int(summary.get("output_tokens") or 0)
            item["known_cost_usd"] += float(summary.get("known_cost_usd") or 0)
            if summary.get("models") and not summary.get("cost_complete", False):
                item["unknown_cost_runs"] += 1
        points = list(buckets.values())
        for item in points:
            item["known_cost_usd"] = round(item["known_cost_usd"], 8)
        return {"days": days, "step": step, "service": service,
                "points": points}

    def ingest(self, spans):
        spans = list(spans)
        with self._connect() as db:
            db.executemany("""INSERT INTO spans
                (trace_id,span_id,parent_span_id,name,start_ns,end_ns,status,service,
                 attributes,events,links,resource_attributes,scope_name)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trace_id,span_id) DO UPDATE SET
                parent_span_id=excluded.parent_span_id, name=excluded.name,
                start_ns=excluded.start_ns, end_ns=excluded.end_ns,
                status=excluded.status, service=excluded.service,
                attributes=excluded.attributes, events=excluded.events,
                links=excluded.links, resource_attributes=excluded.resource_attributes,
                scope_name=excluded.scope_name""",
                [(s["trace_id"], s["span_id"], s["parent_span_id"], s["name"],
                  s["start_unix_nano"], s["end_unix_nano"], s["status"], s["service"],
                  json.dumps(s["attributes"]), json.dumps(s["events"]),
                  json.dumps(s.get("links", [])), json.dumps(s.get("resource_attributes", {})),
                  s.get("scope_name", "")) for s in spans])
            db.executemany("""INSERT INTO trace_activity(trace_id,last_ns) VALUES(?,?)
                ON CONFLICT(trace_id) DO UPDATE SET last_ns=MAX(last_ns,excluded.last_ns)""",
                [(s["trace_id"], s["end_unix_nano"]) for s in spans])
            self._update_endpoints(db, spans)
        self._refresh_after_write(s["trace_id"] for s in spans)
        return len(spans)

    def add_progress(self, trace_id, stage, message, percent=None, session_id=None, service=None):
        trace_id = _id(trace_id, TRACE_ID, "trace_id")
        if service is not None and (not isinstance(service, str) or not 1 <= len(service.strip()) <= 128):
            raise ValueError("service must contain 1 to 128 characters")
        if percent is not None:
            try:
                if isinstance(percent, bool):
                    raise ValueError("percent must be numeric")
                percent = float(percent)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("percent must be between 0 and 100") from exc
            if not math.isfinite(percent) or not 0 <= percent <= 100:
                raise ValueError("percent must be between 0 and 100")
        session_id = _session_value(session_id)
        with self._connect() as db:
            timestamp = time.time_ns()
            db.execute("INSERT INTO progress(trace_id,time_ns,stage,message,percent,session_id,service) VALUES(?,?,?,?,?,?,?)",
                       (trace_id, timestamp, str(stage), str(message), percent, session_id, service.strip() if service else None))
            self._touch_activity(db, trace_id, timestamp)
        self._refresh_after_write([trace_id])

    @staticmethod
    def _touch_activity(db, trace_id, timestamp):
        db.execute("""INSERT INTO trace_activity(trace_id,last_ns) VALUES(?,?)
            ON CONFLICT(trace_id) DO UPDATE SET last_ns=MAX(last_ns,excluded.last_ns)""",
            (trace_id, timestamp))

    def prune(self, retention_days, now_ns=None):
        """Delete complete old traces and uncorrelated telemetry before a cutoff."""
        if retention_days is None or not math.isfinite(float(retention_days)) or float(retention_days) <= 0:
            raise ValueError("retention_days must be positive")
        now_ns = time.time_ns() if now_ns is None else int(now_ns)
        cutoff_ns = now_ns - int(float(retention_days) * 86_400_000_000_000)
        deleted = {}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            stale = """WITH activity(trace_id,activity_ns) AS (
                    SELECT trace_id,end_ns FROM spans
                    UNION ALL SELECT trace_id,time_ns FROM progress
                    UNION ALL SELECT trace_id,time_ns FROM logs WHERE trace_id IS NOT NULL
                    UNION ALL SELECT trace_id,ingested_ns FROM outcomes
                ) SELECT trace_id FROM activity GROUP BY trace_id HAVING MAX(activity_ns) < ?"""
            db.execute("CREATE TEMP TABLE stale_traces(trace_id TEXT PRIMARY KEY)")
            db.execute(f"INSERT INTO stale_traces {stale}", (cutoff_ns,))
            for table in ("spans", "progress", "logs", "outcomes", "endpoint_traces"):
                cursor = db.execute(f"DELETE FROM {table} WHERE trace_id IN (SELECT trace_id FROM stale_traces)")
                deleted[table] = cursor.rowcount
            deleted["trace_activity"] = db.execute(
                "DELETE FROM trace_activity WHERE trace_id IN (SELECT trace_id FROM stale_traces)").rowcount
            deleted["trace_summaries"] = db.execute(
                "DELETE FROM trace_summaries WHERE trace_id IN (SELECT trace_id FROM stale_traces)").rowcount
            deleted["findings"] = db.execute(
                "DELETE FROM finding_cache WHERE trace_id IN (SELECT trace_id FROM stale_traces)").rowcount
            cursor = db.execute("DELETE FROM logs WHERE trace_id IS NULL AND time_ns < ?", (cutoff_ns,))
            deleted["unlinked_logs"] = cursor.rowcount
            cursor = db.execute("DELETE FROM metric_points WHERE time_ns < ?", (cutoff_ns,))
            deleted["metric_points"] = cursor.rowcount
            cursor = db.execute("DELETE FROM endpoint_services WHERE last_ns < ?", (cutoff_ns,))
            deleted["endpoint_services"] = cursor.rowcount
            cursor = db.execute("DELETE FROM endpoints WHERE last_ns < ?", (cutoff_ns,))
            deleted["endpoints"] = cursor.rowcount
            cursor = db.execute("DELETE FROM agent_connections WHERE last_seen_ns < ?", (cutoff_ns,))
            deleted["agent_connections"] = cursor.rowcount
        deleted["cutoff_unix_nano"] = cutoff_ns
        return deleted

    def ingest_logs(self, records):
        records = list(records)
        rows = []
        native_ids = []
        for log in records:
            canonical = json.dumps(log, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            record_id = log['attributes'].get('observatory.record_id')
            if log['scope_name']=='observatory.local_agents' and isinstance(record_id,str) and TRACE_ID.fullmatch(record_id):
                digest = hashlib.sha256(('local-session:'+record_id).encode()).hexdigest()
                native_ids.append((record_id,digest))
            rows.append((digest, log["trace_id"], log["span_id"], log["time_unix_nano"],
                         log["service"], log["severity_number"], log["severity_text"],
                         log["event_name"], json.dumps(log["body"]),
                         json.dumps(log["attributes"]), json.dumps(log["resource_attributes"]),
                         log["scope_name"]))
        with self._connect() as db:
            db.executemany("""DELETE FROM logs WHERE scope_name='observatory.local_agents'
                AND json_extract(attributes,'$."observatory.record_id"')=? AND digest<>?""", native_ids)
            for log in records:
                attrs=log['attributes']
                if log['scope_name']=='observatory.local_agents' and log.get('span_id') and attrs.get('mcp.server.name'):
                    db.execute("""DELETE FROM logs WHERE scope_name='observatory.local_agents'
                        AND trace_id=? AND time_ns=? AND span_id IS NULL
                        AND json_extract(attributes,'$."mcp.server.name"')=?
                        AND json_extract(attributes,'$."agent.activity.action"')=?""",
                        (log['trace_id'],log['time_unix_nano'],attrs['mcp.server.name'],attrs.get('mcp.tool.name')))
            db.executemany("""INSERT INTO logs
                (digest,trace_id,span_id,time_ns,service,severity_number,severity_text,
                 event_name,body,attributes,resource_attributes,scope_name)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(digest) DO UPDATE SET
                span_id=excluded.span_id,service=excluded.service,body=excluded.body,
                attributes=excluded.attributes,resource_attributes=excluded.resource_attributes""", rows)
            db.executemany("""INSERT INTO trace_activity(trace_id,last_ns) VALUES(?,?)
                ON CONFLICT(trace_id) DO UPDATE SET last_ns=MAX(last_ns,excluded.last_ns)""",
                [(log["trace_id"], log["time_unix_nano"]) for log in records if log["trace_id"]])
            self._update_endpoints(db, records)
        self._refresh_after_write(log["trace_id"] for log in records if log["trace_id"])
        return len(rows)

    def logs(self, trace_id=None, limit=100):
        with self._connect() as db:
            if trace_id:
                rows = db.execute("SELECT * FROM logs WHERE trace_id=? ORDER BY time_ns DESC LIMIT ?",
                                  (_id(trace_id, TRACE_ID, "trace_id"), limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM logs ORDER BY time_ns DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r["digest"], "trace_id": r["trace_id"], "span_id": r["span_id"],
                 "time_unix_nano": r["time_ns"], "service": r["service"],
                 "severity_number": r["severity_number"], "severity_text": r["severity_text"],
                 "event_name": r["event_name"], "body": json.loads(r["body"]),
                 "attributes": json.loads(r["attributes"]),
                 "resource_attributes": json.loads(r["resource_attributes"]),
                 "scope_name": r["scope_name"]} for r in rows]

    @staticmethod
    def _event_cursor(cursor):
        if not cursor:
            return None
        try:
            stamp, key = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if not isinstance(stamp, int) or not isinstance(key, str) or len(key) > 512:
                raise ValueError
            return stamp, key
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid cursor") from exc

    @staticmethod
    def _next_event_cursor(stamp, key):
        return base64.urlsafe_b64encode(json.dumps([stamp, key]).encode()).decode().rstrip("=")

    def recent_activity(self, limit=100, service=None, domain=None, query=None, session_id=None):
        return self.activity_page(limit, service, domain, query, session_id)["activity"]

    def activity_page(self, limit=100, service=None, domain=None, query=None, session_id=None,
                      cursor=None, include_reviews=True, errors_only=False):
        """Browse saved activity, including logs older than the trace preview window."""
        limit = max(1, min(500, int(limit)))
        before = self._event_cursor(cursor)
        conditions, params = [], []
        for expression, value in (("s.session_id", session_id),
                                  ("json_extract(s.value,'$.service')", service),
                                  ("json_extract(s.value,'$.domain')", domain)):
            if value:
                conditions.append(expression + "=?")
                params.append(value)
        if not include_reviews:
            conditions.append("COALESCE(json_extract(s.value,'$.service'),'')!='codex-review'")
        if errors_only:
            conditions.append("lower(json_extract(s.value,'$.result')) IN ('error','failed','denied','blocked','policy_denied','incomplete')")
        if query:
            conditions.append("instr(lower(s.value),lower(?))>0")
            params.append(query)
        stamp = "json_extract(s.value,'$.time_unix_nano')"
        key = "COALESCE(s.trace_id,'log') || ':' || json_extract(s.value,'$.source_id')"
        if before:
            conditions.append(f"({stamp},{key}) < (?,?)")
            params.extend(before)
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        # Trace summaries contain span-derived activity. Read log activity from
        # its source table so a busy trace's preview limit cannot hide history.
        def attr(*keys, default="''"):
            fields=[f"NULLIF(json_extract(l.attributes,'$.\"{key}\"'),'')" for key in keys]
            return 'COALESCE(' + ','.join([*fields,default]) + ')'
        fields={
            'time_unix_nano':'l.time_ns', 'service':'l.service', 'source':"'log'",
            'source_id':'l.digest', 'span_id':'l.span_id',
            'domain':attr('agent.activity.domain','activity.domain',default="'other'"),
            'action':attr('agent.activity.action','activity.action',default="'activity'"),
            'subject':attr('agent.activity.subject','agent.activity.target','activity.subject','activity.target'),
            'result':attr('agent.activity.result','activity.result',default="'unknown'"),
            'actor':attr('agent.activity.actor','agent.actor'),
            'mcp_server':attr('mcp.server.name'), 'mcp_tool':attr('mcp.tool.name'),
            'duration_ms':"json_extract(l.attributes,'$.duration_ms')",
            'detail':"CASE WHEN json_type(l.body)='text' THEN json_extract(l.body,'$') ELSE '' END",
        }
        log_value='json_object(' + ','.join(f"'{key}',{value}" for key,value in fields.items()) + ')'
        session=attr(*SESSION_KEYS,default='NULL')
        activity_sql=f"""WITH activity AS (
            SELECT cached.trace_id,cached.session_id,e.value FROM trace_summaries cached,
                json_each(cached.activity_json) e WHERE json_extract(e.value,'$.source')!='log'
            UNION ALL
            SELECT l.trace_id,COALESCE(cached.session_id,{session}),{log_value}
                FROM logs l LEFT JOIN trace_summaries cached ON cached.trace_id=l.trace_id
                WHERE l.event_name='agent.activity'
        )"""
        with self._connect() as db:
            rows = db.execute(activity_sql + f"""SELECT s.trace_id,s.session_id,s.value,{stamp} AS stamp,{key} AS event_key
                FROM activity s
                {where} ORDER BY stamp DESC,event_key DESC LIMIT ?""", [*params, limit+1]).fetchall()
        items = [{**json.loads(row['value']), 'id': row['event_key'],
                  'trace_id': row['trace_id'], 'session_id': row['session_id']} for row in rows[:limit]]
        next_cursor = self._next_event_cursor(rows[limit-1]['stamp'],rows[limit-1]['event_key']) if len(rows)>limit else None
        return {'activity': items, 'next_cursor': next_cursor, 'scope': 'stored activity'}

    def mcp_calls(self, limit=100, service=None, session_id=None, query=None):
        return self.mcp_page(limit, service, session_id, query)['calls']

    def mcp_page(self, limit=100, service=None, session_id=None, query=None, cursor=None,
                 include_reviews=True):
        """Page complete MCP calls, keeping all evidence for each call together."""
        limit = max(1, min(200, int(limit)))
        before = self._event_cursor(cursor)
        match = "(attributes LIKE '%\"mcp.%' OR attributes LIKE '%\"rpc.system\": \"mcp\"%')"
        clauses, params = [match], []
        if service:
            clauses.append("service=?")
            params.append(service)
        if session_id:
            clauses.append("json_extract(attributes,'$.\"session.id\"')=?")
            params.append(session_id)
        if not include_reviews:
            clauses.append("service!='codex-review'")
        where = ' AND '.join(clauses)
        span_key = "trace_id || ':' || span_id"
        log_key = "CASE WHEN trace_id IS NOT NULL AND span_id IS NOT NULL THEN trace_id || ':' || span_id ELSE digest END"
        def mcp_identity(alias):
            return (f"COALESCE(json_extract({alias}.attributes,'$.\"mcp.server.name\"'),'Not reported')",
                    f"COALESCE(json_extract({alias}.attributes,'$.\"mcp.tool.name\"'),"
                    f"json_extract({alias}.attributes,'$.\"tool.name\"'),"
                    f"json_extract({alias}.attributes,'$.\"agent.activity.action\"'),'Not reported')")
        server,tool=mcp_identity('logs')
        linked_server,linked_tool=mcp_identity('linked')
        unique_log=f"""(span_id IS NOT NULL OR NOT EXISTS (
            SELECT 1 FROM logs linked WHERE linked.trace_id=logs.trace_id
            AND linked.time_ns=logs.time_ns AND linked.span_id IS NOT NULL
            AND {linked_server}={server} AND {linked_tool}={tool}))"""
        query_filter = ' HAVING MAX(instr(lower(evidence),lower(?)))>0' if query else ''
        cursor_filter = ' WHERE (stamp,call_key) < (?,?)' if before else ''
        values = [*params, *params, *([query] if query else []), *(before or ()), limit+1]
        with self._connect() as db:
            candidates = db.execute(f"""WITH evidence AS (
                SELECT {span_key} AS call_key,end_ns AS stamp,attributes || name || service AS evidence FROM spans WHERE {where}
                UNION ALL
                SELECT {log_key} AS call_key,time_ns AS stamp,attributes || body || service AS evidence FROM logs WHERE {where} AND {unique_log}
            ), calls AS (
                SELECT call_key,MAX(stamp) AS stamp FROM evidence GROUP BY call_key {query_filter}
            ) SELECT * FROM calls {cursor_filter} ORDER BY stamp DESC,call_key DESC LIMIT ?""", values).fetchall()
            selected_keys = [row['call_key'] for row in candidates[:limit]]
            if not selected_keys:
                return {'calls': [], 'next_cursor': None}
            placeholders = ','.join('?' for _ in selected_keys)
            spans = db.execute(f"SELECT * FROM spans WHERE {span_key} IN ({placeholders})",selected_keys).fetchall()
            logs = db.execute(f"SELECT * FROM logs WHERE {log_key} IN ({placeholders}) ORDER BY time_ns DESC,digest DESC",selected_keys).fetchall()
        calls = {}
        for span in spans:
            attrs = json.loads(span['attributes'])
            key = span['trace_id']+':'+span['span_id']
            calls[key] = {'id': key, 'trace_id': span['trace_id'], 'span_id': span['span_id'],
                'session_id': attrs.get('session.id'), 'service': span['service'],
                'server': attrs.get('mcp.server.name') or attrs.get('server.address') or 'Not reported',
                'tool': attrs.get('mcp.tool.name') or attrs.get('gen_ai.tool.name') or attrs.get('tool.name') or span['name'],
                'method': attrs.get('mcp.method.name') or 'tools/call', 'status': span['status'],
                'time_unix_nano': span['end_ns'],
                'duration_ms': None if attrs.get('observatory.duration_unknown') else max(0, (span['end_ns']-span['start_ns'])/1e6),
                'arguments': attrs.get('input.value') or '', 'output': attrs.get('output.value') or '',
                'source': 'span', 'log_id': None}
        for log in reversed(logs):
            attrs = json.loads(log['attributes'])
            key = log['trace_id']+':'+log['span_id'] if log['trace_id'] and log['span_id'] else log['digest']
            body = json.loads(log['body'])
            try:
                details = json.loads(body) if isinstance(body, str) else body
            except ValueError:
                details = {}
            if not isinstance(details, dict):
                details = {}
            old = calls.get(key, {})
            result = str(attrs.get('agent.activity.result') or '').lower()
            calls[key] = {**old, 'id': key, 'trace_id': log['trace_id'], 'span_id': log['span_id'],
                'session_id': attrs.get('session.id'), 'service': log['service'],
                'server': attrs.get('mcp.server.name') or old.get('server') or 'Not reported',
                'tool': attrs.get('mcp.tool.name') or attrs.get('tool.name') or old.get('tool') or attrs.get('agent.activity.action') or 'Not reported',
                'method': attrs.get('mcp.method.name') or old.get('method') or 'tools/call',
                'status': 'ERROR' if result in {'error','failed','denied','blocked'} else 'RUNNING' if result=='started' else old.get('status') or 'OK',
                'time_unix_nano': log['time_ns'], 'duration_ms': attrs.get('duration_ms', old.get('duration_ms')),
                'arguments': details.get('arguments') or old.get('arguments') or '',
                'output': details.get('result') or old.get('output') or '',
                'source': 'session log + span' if old else 'session log', 'log_id': log['digest']}
        # Older adapters recorded a log without its span ID. Prefer the linked
        # record for the same exact event; preserve distinct explicit spans.
        linked = {(item['trace_id'],item['server'],item['tool'],item['time_unix_nano'])
                  for item in calls.values() if item.get('span_id')}
        items = sorted((item for item in calls.values() if item.get('span_id') or
                        (item['trace_id'],item['server'],item['tool'],item['time_unix_nano']) not in linked),
                       key=lambda item:item['time_unix_nano'], reverse=True)
        by_id = {item['id']: item for item in items}
        items = [by_id[key] for key in selected_keys if key in by_id]
        next_cursor = self._next_event_cursor(candidates[limit-1]['stamp'],candidates[limit-1]['call_key']) if len(candidates)>limit else None
        return {'calls': items, 'next_cursor': next_cursor}

    def ingest_metrics(self, points):
        rows = []
        for point in points:
            canonical = json.dumps(point, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            rows.append((digest, point["time_unix_nano"], point["start_unix_nano"],
                         point["service"], point["name"], point["description"],
                         point["unit"], point["kind"], point["temporality"],
                         json.dumps(point["attributes"]), json.dumps(point["resource_attributes"]),
                         point["scope_name"], json.dumps(point["data"])))
        with self._connect() as db:
            db.executemany("""INSERT OR IGNORE INTO metric_points
                (digest,time_ns,start_ns,service,name,description,unit,kind,
                 temporality,attributes,resource_attributes,scope_name,data)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
            self._update_endpoints(db, points)
        return len(rows)

    def metric_points(self, limit=100, service=None, name=None, kind=None,
                      temporality=None, scope_name=None, unit=None, description=None):
        where = []
        values = []
        if service:
            where.append("service=?")
            values.append(service)
        if name:
            where.append("name=?")
            values.append(name)
        if kind:
            where.append("kind=?")
            values.append(kind)
        if temporality is not None:
            where.append("temporality=?")
            values.append(int(temporality))
        if scope_name is not None:
            where.append("scope_name=?")
            values.append(scope_name)
        if unit is not None:
            where.append("unit=?")
            values.append(unit)
        if description is not None:
            where.append("description=?")
            values.append(description)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._connect() as db:
            rows = db.execute("SELECT * FROM metric_points" + clause +
                              " ORDER BY time_ns DESC LIMIT ?", (*values, limit)).fetchall()
        return [{"id": r["digest"], "time_unix_nano": r["time_ns"],
                 "start_unix_nano": r["start_ns"], "service": r["service"],
                 "name": r["name"], "description": r["description"],
                 "unit": r["unit"], "kind": r["kind"], "temporality": r["temporality"],
                 "attributes": json.loads(r["attributes"]),
                 "resource_attributes": json.loads(r["resource_attributes"]),
                 "scope_name": r["scope_name"], "data": json.loads(r["data"])} for r in rows]

    def metric_catalog(self, limit=200):
        if not 1 <= int(limit) <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as db:
            rows = db.execute("SELECT service,name,description,unit,kind,temporality,scope_name,"
                              "COUNT(*) AS point_count,MAX(time_ns) AS last_ns FROM metric_points "
                              "GROUP BY service,name,description,unit,kind,temporality,scope_name "
                              "ORDER BY last_ns DESC,service,name LIMIT ?", (int(limit),)).fetchall()
        return [dict(row) for row in rows]

    def ingest_outcomes(self, records, source="api"):
        records = list(records)
        now = time.time_ns()
        rows = []
        for record in records:
            trace_id = _id(record.get("trace_id"), TRACE_ID, "trace_id")
            passed = record.get("passed")
            if not isinstance(passed, bool):
                raise ValueError("outcome passed must be a boolean")
            scores = record.get("scores") or []
            if not isinstance(scores, list):
                raise ValueError("outcome scores must be an array")
            rows.append((trace_id, str(record.get("task_id") or ""), int(passed),
                         json.dumps(scores), str(record.get("error") or ""),
                         str(record.get("provider_model") or ""), str(source),
                         json.dumps(record), now))
        with self._connect() as db:
            db.executemany("""INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trace_id) DO UPDATE SET task_id=excluded.task_id,
                passed=excluded.passed,scores=excluded.scores,error=excluded.error,
                provider_model=excluded.provider_model,source=excluded.source,
                details=excluded.details,ingested_ns=excluded.ingested_ns""", rows)
            db.executemany("""INSERT INTO trace_activity(trace_id,last_ns) VALUES(?,?)
                ON CONFLICT(trace_id) DO UPDATE SET last_ns=MAX(last_ns,excluded.last_ns)""",
                [(row[0], now) for row in rows])
        self._refresh_after_write(row[0] for row in rows)
        return len(rows)

    def outcomes(self):
        with self._connect() as db:
            rows = db.execute("SELECT * FROM outcomes").fetchall()
        return {r["trace_id"]: self._outcome_row(r) for r in rows}

    @staticmethod
    def _outcome_row(row):
        return {"trace_id": row["trace_id"], "task_id": row["task_id"],
                "passed": bool(row["passed"]), "scores": json.loads(row["scores"]),
                "error": row["error"], "provider_model": row["provider_model"],
                "source": row["source"], "details": json.loads(row["details"]),
                "ingested_ns": row["ingested_ns"]}

    def _outcome(self, trace_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM outcomes WHERE trace_id=?", (trace_id,)).fetchone()
        return self._outcome_row(row) if row else None

    def _spans(self, trace_id=None):
        with self._connect() as db:
            if trace_id:
                rows = db.execute("SELECT * FROM spans WHERE trace_id=? ORDER BY start_ns,span_id", (trace_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM spans ORDER BY start_ns,span_id").fetchall()
        return [{"trace_id": r["trace_id"], "span_id": r["span_id"],
                 "parent_span_id": r["parent_span_id"], "name": r["name"],
                 "start_unix_nano": r["start_ns"], "end_unix_nano": r["end_ns"],
                 "status": r["status"], "service": r["service"],
                 "attributes": json.loads(r["attributes"]), "events": json.loads(r["events"]),
                 "links": json.loads(r["links"]),
                 "resource_attributes": json.loads(r["resource_attributes"]),
                 "scope_name": r["scope_name"]} for r in rows]

    def span_page(self, trace_id, limit=200, cursor=None, query="", span_filter="all"):
        """Return a bounded, searchable page of spans for one trace."""
        trace_id = _id(trace_id, TRACE_ID, "trace_id")
        limit = int(limit)
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if span_filter not in {"all", "errors", "models", "tools", "agents"}:
            raise ValueError("invalid span filter")
        before = None
        if cursor:
            try:
                decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
                start_ns, span_id = decoded.split(",", 1)
                before = (int(start_ns), _id(span_id, SPAN_ID, "span_id"))
                if before[0] < 0:
                    raise ValueError
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("invalid span cursor") from exc
        clauses = ["trace_id=?"]
        params = [trace_id]
        if query:
            clauses.append("(lower(span_id) LIKE ? ESCAPE '\\' OR lower(name) LIKE ? ESCAPE '\\' OR lower(service) LIKE ? ESCAPE '\\' OR lower(attributes) LIKE ? ESCAPE '\\')")
            needle = "%" + query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params.extend((needle, needle, needle, needle))
        if span_filter == "errors":
            clauses.append("(status='ERROR' OR attributes LIKE '%\"error.type\"%' OR attributes LIKE '%\"exception.type\"%' OR attributes LIKE '%\"tool.error\"%' OR lower(attributes) LIKE '%\"tool.result_status\": \"error%')")
        elif span_filter == "models":
            clauses.append("(attributes LIKE '%\"gen_ai.request.model\"%' OR attributes LIKE '%\"llm.response.model_name\"%' OR attributes LIKE '%\"llm.request.model_name\"%' OR attributes LIKE '%\"llm.model_name\"%' OR attributes LIKE '%\"embedding.model_name\"%' OR attributes LIKE '%\"llm.model\"%' OR attributes LIKE '%\"openinference.span.kind\": \"LLM\"%' OR attributes LIKE '%\"openinference.span.kind\": \"EMBEDDING\"%' OR lower(attributes) LIKE '%\"gen_ai.operation.name\": \"chat%' OR lower(attributes) LIKE '%\"gen_ai.operation.name\": \"text_completion%' OR lower(attributes) LIKE '%\"gen_ai.operation.name\": \"embeddings%')")
        elif span_filter == "tools":
            clauses.append("(attributes LIKE '%\"tool.name\"%' OR attributes LIKE '%\"gen_ai.tool.name\"%' OR attributes LIKE '%\"mcp.tool.name\"%' OR attributes LIKE '%\"openinference.span.kind\": \"TOOL\"%' OR lower(attributes) LIKE '%\"gen_ai.operation.name\": \"execute_tool%' OR lower(name) LIKE '%tool%')")
        elif span_filter == "agents":
            clauses.append("(attributes LIKE '%\"openinference.span.kind\": \"AGENT\"%' OR lower(name) LIKE 'agent.%' OR lower(name) LIKE 'agent-%' OR lower(name) LIKE 'agent_%')")
        where = " AND ".join(clauses)
        cursor_clause = ""
        cursor_params = []
        if before:
            cursor_clause = " AND (start_ns>? OR (start_ns=? AND span_id>?))"
            cursor_params.extend((before[0], before[0], before[1]))
        with self._connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM spans WHERE {where}", params).fetchone()[0]
            rows = db.execute(f"SELECT * FROM spans WHERE {where}{cursor_clause} ORDER BY start_ns,span_id LIMIT ?",
                              [*params, *cursor_params, limit + 1]).fetchall()
            trace_total = db.execute("SELECT COUNT(*) FROM spans WHERE trace_id=?", (trace_id,)).fetchone()[0]
            bounds = db.execute("SELECT MIN(start_ns),MAX(end_ns) FROM spans WHERE trace_id=?", (trace_id,)).fetchone()
        has_more = len(rows) > limit
        visible = rows[:limit]
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = base64.urlsafe_b64encode(f"{last['start_ns']},{last['span_id']}".encode()).decode().rstrip("=")
        spans = [{"trace_id": r["trace_id"], "span_id": r["span_id"],
                  "parent_span_id": r["parent_span_id"], "name": r["name"],
                  "start_unix_nano": r["start_ns"], "end_unix_nano": r["end_ns"],
                  "status": r["status"], "service": r["service"],
                  "attributes": json.loads(r["attributes"]), "events": json.loads(r["events"]),
                  "links": json.loads(r["links"]),
                  "resource_attributes": json.loads(r["resource_attributes"]),
                  "scope_name": r["scope_name"]} for r in visible]
        return {"spans": spans, "total": total, "next_cursor": next_cursor,
                "trace_total": trace_total,
                "start_unix_nano": bounds[0] or 0, "end_unix_nano": bounds[1] or 0}

    def trace(self, trace_id):
        trace_id = _id(trace_id, TRACE_ID, "trace_id")
        spans = self._spans(trace_id)
        with self._connect() as db:
            rows = db.execute("SELECT time_ns,stage,message,percent,session_id,service FROM progress WHERE trace_id=? ORDER BY time_ns,id", (trace_id,)).fetchall()
        progress = [dict(r) for r in rows]
        logs = self.logs(trace_id, limit=1000)
        activity = _activity_for(spans, logs)
        outcome = self._outcome(trace_id)
        for log in logs:
            progress_event = _progress_from_log(log)
            if progress_event is not None:
                progress.append(progress_event)
        progress.sort(key=lambda x: x["time_ns"])
        timeline = progress or [{"time_ns": log["time_unix_nano"]} for log in reversed(logs)]
        summary = summarize(spans) if spans else summarize_progress(trace_id, timeline) if timeline else None
        if summary and not spans and logs:
            summary["services"] = sorted(set(summary["services"]) | {log["service"] for log in logs})
        if outcome:
            summary = apply_outcome(summary or summarize_progress(trace_id, [{"time_ns": outcome["ingested_ns"]}]), outcome)
        session_id = _session_for(spans, progress, logs)
        if summary:
            summary["session_id"] = session_id
            summary["activity_terms"] = sorted({str(value)[:200] for item in activity
                for value in (item["domain"], item["action"], item["subject"], item["actor"]) if value})
            searchable_attributes = ("tool.name", "tool.type", "gen_ai.tool.name", "gen_ai.tool.type",
                                    "exception.type", "error.type", "gen_ai.provider.name",
                                    "gen_ai.operation.name",
                                    "openai_agents.workflow", "mcp.method.name", "mcp.server.name",
                                    "db.system", "http.request.method")
            summary["span_terms"] = sorted({str(value)[:200] for span in spans
                for value in (span["name"], *(span["attributes"].get(key)
                    for key in searchable_attributes))
                if isinstance(value, (str, int, float)) and str(value).strip()})
            summary["log_terms"] = sorted({str(value)[:200] for log in logs
                for value in (log.get("event_name"), log.get("severity_text")) if value})
        return {"trace_id": trace_id, "spans": spans, "progress": progress, "logs": logs,
                "activity": activity,
                "outcome": outcome, "findings": self._trace_findings(trace_id),
                "session_id": session_id, "summary": summary}

    def trace_overview(self, trace_id):
        """Load trace panels from derived indexes without decoding every span."""
        trace_id = _id(trace_id, TRACE_ID, "trace_id")
        with self._connect() as db:
            row = db.execute("SELECT summary_json,activity_json FROM trace_summaries WHERE trace_id=?",
                             (trace_id,)).fetchone()
        if row is None:
            self._refresh_summaries([trace_id])
            with self._connect() as db:
                row = db.execute("SELECT summary_json,activity_json FROM trace_summaries WHERE trace_id=?",
                                 (trace_id,)).fetchone()
        with self._connect() as db:
            rows = db.execute("SELECT time_ns,stage,message,percent,session_id,service FROM progress "
                              "WHERE trace_id=? ORDER BY time_ns,id", (trace_id,)).fetchall()
        progress = [dict(item) for item in rows]
        logs = self.logs(trace_id, limit=1000)
        for log in logs:
            event = _progress_from_log(log)
            if event is not None:
                progress.append(event)
        progress.sort(key=lambda item: item["time_ns"])
        summary = json.loads(row["summary_json"]) if row else None
        outcome = self._outcome(trace_id)
        if outcome:
            summary = apply_outcome(summary or summarize_progress(trace_id,
                [{"time_ns": outcome["ingested_ns"]}]), outcome)
        return {"trace_id": trace_id, "spans": [], "progress": progress, "logs": logs,
                "activity": json.loads(row["activity_json"]) if row else [],
                "outcome": outcome, "findings": self._trace_findings(trace_id),
                "session_id": summary.get("session_id") if summary else None,
                "summary": summary}

    def findings(self, limit=100):
        return self.finding_page(limit=limit)["findings"]

    def finding_page(self, limit=100, cursor=None):
        if not 1 <= int(limit) <= 500:
            raise ValueError("limit must be between 1 and 500")
        before = None
        if cursor:
            try:
                decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
                before_ns, before_id = decoded.split(",", 1)
                before = (int(before_ns), before_id)
                if before[0] < 0 or not re.fullmatch(r"[0-9a-f]{16}", before_id):
                    raise ValueError
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("invalid cursor") from exc
        sql = "SELECT finding_id,time_ns,finding_json FROM finding_cache"
        params = []
        if before:
            sql += " WHERE time_ns < ? OR (time_ns = ? AND finding_id < ?)"
            params.extend((before[0], before[0], before[1]))
        sql += " ORDER BY time_ns DESC,finding_id DESC LIMIT ?"
        params.append(int(limit) + 1)
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        has_more = len(rows) > int(limit)
        visible = rows[:int(limit)]
        next_cursor = None
        if has_more and visible:
            row = visible[-1]
            next_cursor = base64.urlsafe_b64encode(
                f"{row['time_ns']},{row['finding_id']}".encode()).decode().rstrip("=")
        return {"findings": [json.loads(row["finding_json"]) for row in visible],
                "next_cursor": next_cursor, "has_more": has_more}

    def _trace_findings(self, trace_id):
        with self._connect() as db:
            rows = db.execute("SELECT finding_json FROM finding_cache WHERE trace_id=? "
                              "ORDER BY time_ns DESC,finding_id", (trace_id,)).fetchall()
        return [json.loads(row["finding_json"]) for row in rows]

    def sessions(self, limit=100):
        if not 1 <= int(limit) <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as db:
            rows = db.execute("""SELECT s.session_id,s.trace_id,s.summary_json,a.last_ns
                FROM trace_summaries AS s JOIN trace_activity AS a USING(trace_id)
                WHERE s.session_id IN (
                    SELECT s2.session_id FROM trace_summaries AS s2
                    JOIN trace_activity AS a2 USING(trace_id)
                    WHERE s2.session_id IS NOT NULL GROUP BY s2.session_id
                    ORDER BY MAX(a2.last_ns) DESC,s2.session_id DESC LIMIT ?)
                ORDER BY a.last_ns DESC,s.session_id DESC""", (int(limit),)).fetchall()
        grouped = {}
        for row in rows:
            item = grouped.setdefault(row["session_id"], {
                "session_id": row["session_id"], "trace_count": 0, "errors": 0,
                "failed": 0, "incomplete": 0, "issues": 0,
                "input_tokens": 0, "output_tokens": 0,
                "known_cost_usd": 0.0, "unknown_cost_runs": 0, "cost_complete": True,
                "services": set(), "models": set(), "latest_trace_id": row["trace_id"],
                "last_unix_nano": row["last_ns"]})
            summary = json.loads(row["summary_json"])
            item["trace_count"] += 1
            status = summary.get("status")
            item["errors"] += status == "ERROR"
            item["failed"] += status == "FAILED" or summary.get("outcome_passed") is False
            item["incomplete"] += status == "INCOMPLETE"
            item["issues"] += status in {"ERROR", "FAILED", "INCOMPLETE"} or summary.get("outcome_passed") is False
            item["input_tokens"] += int(summary.get("input_tokens") or 0)
            item["output_tokens"] += int(summary.get("output_tokens") or 0)
            item["known_cost_usd"] += float(summary.get("known_cost_usd") or 0)
            cost_complete = bool(summary.get("cost_complete", False))
            item["unknown_cost_runs"] += not cost_complete
            item["cost_complete"] &= cost_complete
            item["services"].update(summary.get("services", []))
            item["models"].update(summary.get("models", []))
        result = []
        for item in grouped.values():
            item["known_cost_usd"] = round(item["known_cost_usd"], 8)
            item["services"] = sorted(item["services"])
            item["models"] = sorted(item["models"])
            result.append(item)
        return result

    def related_traces(self, trace_id):
        trace_id = _id(trace_id, TRACE_ID, "trace_id")
        relations = {}
        groups = {}
        for span in self._spans():
            groups.setdefault(span["trace_id"], []).append(span)
            for link in span["links"]:
                target = link["trace_id"]
                if span["trace_id"] == trace_id and target != trace_id:
                    relations.setdefault((target, "outgoing"), []).append({
                        "source_span_id": span["span_id"], "linked_span_id": link["span_id"]})
                if target == trace_id and span["trace_id"] != trace_id:
                    relations.setdefault((span["trace_id"], "incoming"), []).append({
                        "source_span_id": span["span_id"], "linked_span_id": link["span_id"]})
        return [{"trace_id": target, "direction": direction, "edges": edges,
                 "summary": summarize(groups[target]) if target in groups else None}
                for (target, direction), edges in sorted(relations.items())]

    def trace_page(self, limit=100, service=None, status=None, cursor=None, session_id=None,
                   search=None, endpoint_id=None, include_reviews=True):
        if not 1 <= int(limit) <= 500:
            raise ValueError("limit must be between 1 and 500")
        if session_id is not None:
            session_id = _session_value(session_id)
            if session_id is None:
                raise ValueError("invalid session_id")
        search = str(search or "").strip()[:200].casefold()
        if endpoint_id is not None:
            if not isinstance(endpoint_id, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", endpoint_id):
                raise ValueError("invalid endpoint_id")
            endpoint_id = endpoint_id.lower()
        before = None
        if cursor:
            try:
                decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
                before_ns, before_id = decoded.split(",", 1)
                before = (int(before_ns), _id(before_id, TRACE_ID, "cursor trace_id"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("invalid cursor") from exc

        selected = []
        page_cursor = before
        batch_size = max(50, min(250, int(limit) * 2))
        exhausted = False
        while len(selected) <= int(limit):
            sql = "SELECT a.trace_id,a.last_ns,s.summary_json FROM trace_activity AS a " \
                  "LEFT JOIN trace_summaries AS s ON s.trace_id=a.trace_id"
            conditions = []
            params = []
            if page_cursor:
                conditions.append("(a.last_ns < ? OR (a.last_ns = ? AND a.trace_id < ?))")
                params.extend((page_cursor[0], page_cursor[0], page_cursor[1]))
            if session_id is not None:
                conditions.append("s.session_id=?")
                params.append(session_id)
            if endpoint_id is not None:
                conditions.append("EXISTS (SELECT 1 FROM endpoint_traces et "
                                  "WHERE et.trace_id=a.trace_id AND et.endpoint_id=?)")
                params.append(endpoint_id)
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY a.last_ns DESC,a.trace_id DESC LIMIT ?"
            params.append(batch_size)
            with self._connect() as db:
                rows = db.execute(sql, params).fetchall()
            if not rows:
                exhausted = True
                break
            for row in rows:
                activity = (row["last_ns"], row["trace_id"])
                page_cursor = activity
                if row["summary_json"] is None:
                    if self._async_derived_refresh:
                        self._refresh_after_write([row["trace_id"]])
                        summary = None
                    else:
                        self._refresh_summaries([row["trace_id"]])
                        with self._connect() as db:
                            cached = db.execute("SELECT summary_json FROM trace_summaries WHERE trace_id=?",
                                                (row["trace_id"],)).fetchone()
                        summary = json.loads(cached[0]) if cached else None
                else:
                    summary = json.loads(row["summary_json"])
                if not summary:
                    continue
                if session_id is not None and summary.get("session_id") != session_id:
                    continue
                if service and service not in summary["services"]:
                    continue
                if not include_reviews and 'codex-review' in summary['services']:
                    continue
                if status and summary["status"] != status:
                    continue
                if search:
                    searchable = " ".join(str(value) for value in (
                        summary.get("trace_id"), summary.get("name"), summary.get("task"),
                        summary.get("session_id"), *summary.get("services", []),
                        *summary.get("models", []), *summary.get("activity_terms", []),
                        *summary.get("span_terms", []), *summary.get("log_terms", [])))
                    if search not in searchable.casefold():
                        continue
                selected.append((summary, activity))
                if len(selected) > int(limit):
                    break
            if len(selected) > int(limit):
                break
            if len(rows) < batch_size:
                exhausted = True
                break
        has_more = len(selected) > int(limit)
        visible = selected[:int(limit)]
        next_cursor = None
        if has_more and visible:
            ns, trace_id = visible[-1][1]
            next_cursor = base64.urlsafe_b64encode(f"{ns},{trace_id}".encode()).decode().rstrip("=")
        return {"traces": [item[0] for item in visible],
                "next_cursor": next_cursor, "has_more": has_more or not exhausted}

    def traces(self, limit=100, service=None, status=None, cursor=None, session_id=None,
               search=None, endpoint_id=None):
        return self.trace_page(limit, service, status, cursor, session_id, search,
                               endpoint_id)["traces"]

    def metrics(self):
        totals = {"runs": 0, "errors": 0, "incomplete": 0, "running": 0,
                  "spans": 0, "input_tokens": 0, "output_tokens": 0,
                  "known_cost_usd": 0.0, "unknown_cost_runs": 0}
        cursor = None
        while True:
            page = self.trace_page(limit=500, cursor=cursor)
            for run in page["traces"]:
                totals["runs"] += 1
                totals["errors"] += run["status"] == "ERROR"
                totals["incomplete"] += run["status"] == "INCOMPLETE"
                totals["running"] += run["status"] == "RUNNING"
                totals["spans"] += run["span_count"]
                totals["input_tokens"] += run["input_tokens"]
                totals["output_tokens"] += run["output_tokens"]
                totals["known_cost_usd"] += run["known_cost_usd"]
                totals["unknown_cost_runs"] += not run["cost_complete"]
            cursor = page["next_cursor"]
            if not cursor:
                break
        totals["known_cost_usd"] = round(totals["known_cost_usd"], 8)
        with self._connect() as db:
            totals["failed_outcomes"] = db.execute(
                "SELECT COUNT(*) FROM outcomes WHERE passed=0").fetchone()[0]
        totals.update({
                "logs": self._log_count(),
                "metric_points": self._metric_count(),
                "services": self._services()}
        )
        return totals

    def _log_count(self):
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM logs").fetchone()[0]

    def _metric_count(self):
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM metric_points").fetchone()[0]

    def _services(self):
        with self._connect() as db:
            rows = db.execute("""SELECT service FROM spans UNION
                SELECT service FROM logs UNION SELECT service FROM metric_points UNION
                SELECT service FROM progress WHERE service IS NOT NULL""").fetchall()
        return sorted(row[0] for row in rows)


def summarize_progress(trace_id, progress):
    start, end = progress[0]["time_ns"], progress[-1]["time_ns"]
    return {"trace_id": trace_id, "name": "agent.run", "task": None,
            "outcome": None, "steps": None, "summary": None,
            "services": sorted({p["service"] for p in progress if p.get("service")}),
            "start_unix_nano": start, "end_unix_nano": end,
            "duration_ms": round((end - start) / 1e6, 3), "status": "RUNNING",
            "span_count": 0, "input_tokens": 0, "output_tokens": 0,
            "known_cost_usd": 0.0, "cost_complete": False, "models": []}


def apply_outcome(summary, outcome):
    summary["task_id"] = outcome["task_id"]
    summary["outcome_passed"] = outcome["passed"]
    if not outcome["passed"] and summary["status"] != "ERROR":
        summary["status"] = "FAILED"
    elif outcome["passed"] and summary["status"] == "RUNNING":
        summary["status"] = "OK"
    return summary


def summarize(spans):
    roots = [s for s in spans if not s["parent_span_id"]]
    root = min(roots or spans, key=lambda s: s["start_unix_nano"])
    starts = [s["start_unix_nano"] for s in spans]
    ends = [s["end_unix_nano"] for s in spans]
    # Run-level totals duplicate child LLM usage; only count leaf model calls.
    usage_keys = ("gen_ai.usage.input_tokens", "llm.usage.input_tokens",
                  "gen_ai.usage.output_tokens", "llm.usage.output_tokens",
                  "llm.token_count.prompt", "llm.token_count.completion")
    llms = [s for s in spans if s["name"] != "agent.run" and
            (any(k in s["attributes"] for k in usage_keys) or
             any(k.startswith("llm.cost.") for k in s["attributes"]) or
             "llm.cost_usd" in s["attributes"] or "gen_ai.usage.cost" in s["attributes"])]
    def numeric(a, key):
        try:
            value = float(a[key])
            return value if math.isfinite(value) else None
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
    def num(a, keys):
        for key in keys:
            if key in a:
                value = numeric(a, key)
                return value if value is not None else 0
        return 0
    def cost_info(attrs):
        for key in ("llm.cost.total", "llm.cost_usd", "gen_ai.usage.cost"):
            if key in attrs:
                value = numeric(attrs, key)
                return value, value is not None and attrs.get("llm.cost_known", True)
        component_keys = ("llm.cost.prompt", "llm.cost.completion")
        if any(key in attrs for key in component_keys):
            values = [numeric(attrs, key) for key in component_keys]
            valid = [value for value in values if value is not None]
            return (sum(valid) if valid else None,
                    len(valid) == len(component_keys) and attrs.get("llm.cost_known", True))
        return None, False
    input_tokens = sum(num(s["attributes"], ("gen_ai.usage.input_tokens", "llm.usage.input_tokens", "llm.token_count.prompt")) for s in llms)
    output_tokens = sum(num(s["attributes"], ("gen_ai.usage.output_tokens", "llm.usage.output_tokens", "llm.token_count.completion")) for s in llms)
    call_costs = [cost_info(s["attributes"]) for s in llms]
    cost_complete = len(call_costs) == len(llms) and all(complete for _, complete in call_costs)
    outcome = root["attributes"].get("agent.stopped_reason")
    state = ("ERROR" if any(s["status"] == "ERROR" or
                            s["attributes"].get("tool.result_status") == "error" or
                            bool(s["attributes"].get("tool.error")) for s in spans) else
             "INCOMPLETE" if outcome and outcome != "completed" else
             "RUNNING" if any(s["name"] == "agent.run" and s["attributes"].get("agent.run.state") == "running" for s in spans) else "OK")
    return {"trace_id": root["trace_id"], "name": root["name"],
            "active": any(s['name']=='agent.run' and s['attributes'].get('agent.run.state')=='running' for s in spans),
            "task": root["attributes"].get("agent.task"),
            "outcome": outcome, "steps": root["attributes"].get("agent.steps"),
            "summary": root["attributes"].get("agent.summary"),
            "services": sorted({s["service"] for s in spans}),
            "start_unix_nano": min(starts), "end_unix_nano": max(ends),
            "duration_ms": round((max(ends) - min(starts)) / 1e6, 3),
            "status": state,
            "span_count": len(spans), "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "known_cost_usd": round(sum(value for value, _ in call_costs if value is not None), 8),
            "cost_complete": cost_complete,
            "models": sorted({str(model) for s in spans
                              if (model := s["attributes"].get("gen_ai.request.model") or
                                  s["attributes"].get("llm.response.model_name") or
                                  s["attributes"].get("llm.request.model_name") or
                                  s["attributes"].get("llm.model_name") or
                                  s["attributes"].get("embedding.model_name") or
                                  s["attributes"].get("llm.model")) is not None})}
