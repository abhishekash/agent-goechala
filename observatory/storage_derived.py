"""Bounded evidence and exact aggregates for the derived trace index.

Callers hold a SQLite write transaction for the entire read/replace operation.
Temporary usage rows live in SQLite, not a Python object graph of the trace.
"""
import json
import math


EVIDENCE_LIMIT = 256
RECORD_BYTES = 64 * 1024
WINDOW_BYTES = 2 * 1024 * 1024
GROUP_LIMIT = 128
LABEL_CHARS = 128


def finalize_usage(item):
    """Keep a sampled/unsupported run from becoming false aggregate zeros."""
    if not item.get("usage_complete", True):
        for field in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                      "cache_write_input_tokens", "reasoning_output_tokens"):
            item[field] = None
            item[field + "_reported"] = None
        item["known_cost_usd"] = None
        item["cost_complete"] = False


def merge_labels(item, field, labels, complete=True):
    values = item[field]
    for label in labels:
        label = str(label)
        if len(label) > LABEL_CHARS:
            complete = False
        values.add(label[:LABEL_CHARS])
        if len(values) > GROUP_LIMIT:
            values.remove(max(values))
            complete = False
    item[field + "_complete"] = item.get(field + "_complete", True) and complete


def _bounded_columns(table):
    payloads = (("attributes", "events", "links", "resource_attributes") if table == "spans"
                else ("attributes", "body", "resource_attributes"))
    size = "+".join(f"length(CAST({key} AS BLOB))" for key in payloads)
    metadata = ("trace_id,span_id,parent_span_id,substr(name,1,256) AS name,start_ns,end_ns,status,"
                "substr(service,1,128) AS service,substr(scope_name,1,256) AS scope_name"
                if table == "spans" else
                "digest,trace_id,time_ns,severity_number,substr(severity_text,1,128) AS severity_text,"
                "substr(event_name,1,256) AS event_name,substr(service,1,128) AS service")
    columns = [metadata, f"({size}) AS payload_bytes", f"({size})<={RECORD_BYTES} AS payload_complete"]
    for key in payloads:
        empty = "[]" if key in {"events", "links"} else "null" if key == "body" else "{}"
        columns.append(f"CASE WHEN ({size})<={RECORD_BYTES} THEN {key} ELSE '{empty}' END AS {key}")
    return ",".join(columns)


def initialize(db):
    """Cache compact per-span calculations; invalidate in the source transaction.

    Parent/child deduplication is deliberately recomputed: late children and
    reparenting can change which previously seen usage records are leaves.
    """
    db.execute("""CREATE TABLE IF NOT EXISTS derived_span_stats_v1 (
        trace_id TEXT NOT NULL, span_id TEXT NOT NULL, stats TEXT NOT NULL,
        PRIMARY KEY(trace_id,span_id))""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS derived_stats_update AFTER UPDATE ON spans
        WHEN OLD.attributes != NEW.attributes OR OLD.name != NEW.name OR OLD.status != NEW.status
        OR OLD.trace_id != NEW.trace_id OR OLD.span_id != NEW.span_id
        BEGIN
            DELETE FROM derived_span_stats_v1 WHERE trace_id=OLD.trace_id AND span_id=OLD.span_id;
            DELETE FROM derived_span_stats_v1 WHERE trace_id=NEW.trace_id AND span_id=NEW.span_id;
        END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS derived_stats_delete AFTER DELETE ON spans
        BEGIN DELETE FROM derived_span_stats_v1 WHERE trace_id=OLD.trace_id AND span_id=OLD.span_id; END""")


def session_for_trace(db, trace_id):
    """Resolve identity over all sources, independently of evidence sampling."""
    from .core import SESSION_KEYS, _session_value

    db.create_function("derived_session_value", 1, _session_value)
    def source_value(key):
        path = "'$.\"" + key + "\"'"
        value = f"json_extract(attributes,{path})"
        kind = f"json_type(attributes,{path})"
        empty = (f"({kind} IS NULL OR {kind}='null' OR {kind}='false' OR "
                 f"({kind} IN ('integer','real') AND {value}=0) OR "
                 f"({kind}='text' AND {value}='') OR ({kind}='object' AND {value}='{{}}') OR "
                 f"({kind}='array' AND {value}='[]'))")
        raw = f"CASE WHEN {empty} THEN json_extract(resource_attributes,{path}) ELSE {value} END"
        raw_kind = f"CASE WHEN {empty} THEN json_type(resource_attributes,{path}) ELSE {kind} END"
        return f"derived_session_value(CASE WHEN ({raw_kind})='text' AND length({raw})<=512 THEN {raw} END)"

    for key in SESSION_KEYS:
        row = db.execute(f"""SELECT value FROM (
            SELECT parent_span_id,start_ns,span_id,0 AS position,
                {source_value(key)} AS value
                FROM spans WHERE trace_id=?
            UNION ALL
            SELECT s.parent_span_id,s.start_ns,s.span_id,CAST(e.key AS INTEGER)+1,
                derived_session_value(CASE WHEN json_type(
                    CASE WHEN e.type='object' THEN e.value ELSE '{{}}' END,?)='text'
                    AND length(json_extract(CASE WHEN e.type='object' THEN e.value ELSE '{{}}' END,?))<=512
                    THEN json_extract(CASE WHEN e.type='object' THEN e.value ELSE '{{}}' END,?) END)
                FROM spans s,json_each(s.events) e WHERE s.trace_id=?
            ) WHERE value IS NOT NULL
            ORDER BY CASE WHEN parent_span_id IS NULL OR parent_span_id='' THEN 0 ELSE 1 END,
                start_ns,span_id,position LIMIT 1""", (trace_id, '$.attributes."' + key + '"',
                                                      '$.attributes."' + key + '"',
                                                      '$.attributes."' + key + '"', trace_id)).fetchone()
        if row:
            return row[0]
        row = db.execute(f"""SELECT {source_value(key)} AS value FROM logs
            WHERE trace_id=? AND value IS NOT NULL ORDER BY time_ns DESC,digest DESC LIMIT 1""",
                         (trace_id,)).fetchone()
        if row:
            return row[0]
        if key == "session.id":
            row = db.execute("""SELECT derived_session_value(
                CASE WHEN length(session_id)<=512 THEN session_id END) AS value FROM progress
                WHERE trace_id=? AND value IS NOT NULL ORDER BY time_ns,id LIMIT 1""", (trace_id,)).fetchone()
            if row:
                return row[0]
    return None


def span_record(row):
    return {"trace_id": row["trace_id"], "span_id": row["span_id"],
            "parent_span_id": row["parent_span_id"], "name": row["name"],
            "start_unix_nano": row["start_ns"], "end_unix_nano": row["end_ns"],
            "status": row["status"], "service": row["service"],
            **{key: json.loads(row[key]) for key in
               ("attributes", "events", "links", "resource_attributes")},
            "scope_name": row["scope_name"]}


def _number(attrs, key):
    try:
        value = float(attrs[key])
        return value if math.isfinite(value) else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _compact_statistics(values):
    """Most tool spans have no usage: do not persist a dictionary of zeros."""
    return json.dumps({key: value for key, value in values.items()
                       if key == 'mask' or value not in (None, False, 0)},
                      separators=(',', ':'))


def aggregate_spans(db, trace_id, summary):
    from .core import INPUT_TOKEN_KEYS, OUTPUT_TOKEN_KEYS, TOKEN_BREAKDOWN_KEYS
    from .findings import mcp_result_error

    fields = {"input_tokens": INPUT_TOKEN_KEYS, "output_tokens": OUTPUT_TOKEN_KEYS,
              **TOKEN_BREAKDOWN_KEYS}
    cost_bit = 1 << len(fields)

    def statistics(raw, name, status):
        if raw is None:
            return _compact_statistics({"mask": 0, "unsupported": True, "error": status == "ERROR"})
        attrs = json.loads(raw)
        mask = 0
        values = {}
        for index, (field, keys) in enumerate(fields.items()):
            key = next((key for key in keys if key in attrs), None)
            values[field] = (_number(attrs, key) or 0) if key else 0
            if name != "agent.run" and key:
                mask |= 1 << index
        cost, complete = None, False
        for key in ("llm.cost.total", "llm.cost_usd", "gen_ai.usage.cost"):
            if key in attrs:
                cost = _number(attrs, key)
                complete = cost is not None and bool(attrs.get("llm.cost_known", True))
                break
        else:
            keys = ("llm.cost.prompt", "llm.cost.completion")
            if any(key in attrs for key in keys):
                parts = [_number(attrs, key) for key in keys]
                valid = [part for part in parts if part is not None]
                cost = sum(valid) if valid else None
                complete = len(valid) == 2 and bool(attrs.get("llm.cost_known", True))
        if name != "agent.run" and (any(key.startswith("llm.cost.") for key in attrs)
                                   or "llm.cost_usd" in attrs or "gen_ai.usage.cost" in attrs):
            mask |= cost_bit
        model = next((attrs[key] for key in (
            "gen_ai.request.model", "llm.response.model_name", "llm.request.model_name",
            "llm.model_name", "embedding.model_name", "llm.model") if attrs.get(key)), None)
        model = str(model) if model is not None else None
        values.update(mask=mask, cost=cost, complete=complete, unsupported=False,
                      model=model[:LABEL_CHARS] if model is not None else None,
                      model_truncated=model is not None and len(model) > LABEL_CHARS,
                      error=bool(status == "ERROR" or attrs.get("tool.result_status") == "error"
                                 or attrs.get("tool.error") or mcp_result_error(attrs)),
                      active=name == "agent.run" and attrs.get("agent.run.state") == "running")
        return _compact_statistics(values)

    db.create_function("derived_statistics", 3, statistics)
    db.execute("""INSERT INTO derived_span_stats_v1(trace_id,span_id,stats)
        SELECT s.trace_id,s.span_id,derived_statistics(
            CASE WHEN length(CAST(s.attributes AS BLOB))<=65536 THEN s.attributes ELSE NULL END,
            substr(s.name,1,256),s.status)
        FROM spans s WHERE s.trace_id=? AND NOT EXISTS (
            SELECT 1 FROM derived_span_stats_v1 d WHERE d.trace_id=s.trace_id AND d.span_id=s.span_id)
        """, (trace_id,))
    db.execute("DROP TABLE IF EXISTS temp.derived_usage")
    db.execute("""CREATE TEMP TABLE derived_usage (
        span_id TEXT PRIMARY KEY, parent_span_id TEXT, stats TEXT, leaves INTEGER)""")
    db.execute("""INSERT INTO derived_usage(span_id,parent_span_id,stats)
        SELECT s.span_id,s.parent_span_id,d.stats FROM spans s
        JOIN derived_span_stats_v1 d ON d.trace_id=s.trace_id AND d.span_id=s.span_id
        WHERE s.trace_id=?""", (trace_id,))
    db.execute("UPDATE derived_usage SET leaves=json_extract(stats,'$.mask')")
    # UNION (not UNION ALL) terminates malformed cyclic parent graphs as well.
    for index in range(len(fields) + 1):
        bit = 1 << index
        db.execute("""WITH RECURSIVE ancestors(ancestor) AS (
            SELECT parent_span_id FROM derived_usage WHERE leaves & ? != 0
            UNION SELECT p.parent_span_id FROM ancestors a
                JOIN derived_usage p ON p.span_id=a.ancestor
        ) UPDATE derived_usage SET leaves=leaves & ~?
            WHERE span_id IN (SELECT ancestor FROM ancestors)""", (bit, bit))
    totals = db.execute("""SELECT COUNT(*) AS count,MIN(start_ns) AS start,
        MAX(end_ns) AS end FROM spans WHERE trace_id=?""", (trace_id,)).fetchone()
    summary.update(span_count=totals["count"], start_unix_nano=totals["start"],
                   end_unix_nano=totals["end"],
                   duration_ms=round((totals["end"] - totals["start"]) / 1e6, 3))
    for index, field in enumerate(fields):
        row = db.execute(f"""SELECT COUNT(*) AS count,
            SUM(json_extract(stats,'$.{field}')) AS total
            FROM derived_usage WHERE leaves & ? != 0""", (1 << index,)).fetchone()
        summary[field] = int(row["total"] or 0)
        summary[field + "_reported"] = bool(row["count"])
    row = db.execute("""SELECT
        SUM(CASE WHEN leaves & ? != 0 THEN json_extract(stats,'$.cost') ELSE 0 END) AS cost,
        SUM(leaves & 3 != 0) AS usage_count,
        SUM(leaves & ? != 0) AS cost_count,
        SUM(CASE WHEN leaves & ? != 0 AND NOT COALESCE(json_extract(stats,'$.complete'),0) THEN 1 ELSE 0 END) AS unknown,
        MAX(json_extract(stats,'$.error')) AS error,
        MAX(json_extract(stats,'$.active')) AS active,
        MAX(json_extract(stats,'$.unsupported')) AS unsupported,
        MAX(json_extract(stats,'$.model_truncated')) AS model_truncated FROM derived_usage""",
                     (cost_bit, cost_bit, cost_bit)).fetchone()
    summary["known_cost_usd"] = round(row["cost"] or 0, 8)
    summary["cost_complete"] = bool(row["usage_count"] and
                                    row["usage_count"] == row["cost_count"] and not row["unknown"])
    summary["active"] = bool(row["active"])
    summary["status"] = ("ERROR" if row["error"] else "INCOMPLETE"
                         if summary.get("outcome") and summary["outcome"] != "completed"
                         else "UNKNOWN" if row["unsupported"] else "RUNNING" if row["active"] else "OK")
    summary["usage_complete"] = not bool(row["unsupported"])
    summary["status_complete"] = not bool(row["unsupported"])
    if row["unsupported"]:
        for field in fields:
            summary[field] = None
            summary[field + "_reported"] = None
        summary["known_cost_usd"] = None
        summary["cost_complete"] = False
        summary["active"] = None
    services = db.execute("""SELECT substr(service,1,128),length(service)<=128
        FROM spans WHERE trace_id=? GROUP BY service ORDER BY service LIMIT ?""",
                          (trace_id, GROUP_LIMIT + 1)).fetchall()
    summary["services"] = [r[0] for r in services[:GROUP_LIMIT]]
    summary["services_complete"] = len(services) <= GROUP_LIMIT and all(r[1] for r in services)
    models = [r[0] for r in db.execute("""SELECT DISTINCT json_extract(stats,'$.model')
        FROM derived_usage WHERE json_extract(stats,'$.model') IS NOT NULL ORDER BY 1 LIMIT ?""",
                                      (GROUP_LIMIT + 1,))]
    summary["models"] = models[:GROUP_LIMIT]
    summary["models_complete"] = (len(models) <= GROUP_LIMIT and not row["unsupported"]
                                  and not row["model_truncated"])
    db.execute("DROP TABLE derived_usage")


def derive_trace(db, trace_id):
    # Lazy import keeps the existing public normalization/summary API unchanged.
    from .core import (summarize, summarize_progress, apply_outcome, _activity_for,
                       _progress_from_log)
    from .findings import analyze

    spans = []
    payload_complete = True
    used = 0
    for row in db.execute(f"SELECT {_bounded_columns('spans')} FROM spans WHERE trace_id=? "
                          "ORDER BY start_ns DESC,span_id DESC LIMIT ?", (trace_id, EVIDENCE_LIMIT)):
        used += min(row["payload_bytes"], RECORD_BYTES)
        if used > WINDOW_BYTES:
            payload_complete = False
            break
        payload_complete = payload_complete and bool(row["payload_complete"])
        spans.append(span_record(row))
    root = db.execute(f"""SELECT {_bounded_columns('spans')} FROM spans WHERE trace_id=?
        ORDER BY CASE WHEN parent_span_id IS NULL OR parent_span_id='' THEN 0 ELSE 1 END,
        start_ns,span_id LIMIT 1""", (trace_id,)).fetchone()
    if root and all(span["span_id"] != root["span_id"] for span in spans):
        spans.append(span_record(root))
    if root:
        payload_complete = payload_complete and bool(root["payload_complete"])
    spans.sort(key=lambda span: (span["start_unix_nano"], span["span_id"]))
    logs = []
    used = 0
    for row in db.execute(f"SELECT {_bounded_columns('logs')} FROM logs WHERE trace_id=? ORDER BY time_ns DESC,digest DESC LIMIT ?",
                          (trace_id, EVIDENCE_LIMIT)):
        used += min(row["payload_bytes"], RECORD_BYTES)
        if used > WINDOW_BYTES:
            payload_complete = False
            break
        payload_complete = payload_complete and bool(row["payload_complete"])
        logs.append({"id": row["digest"], "trace_id": trace_id, "time_unix_nano": row["time_ns"],
                     "severity_number": row["severity_number"], "severity_text": row["severity_text"],
                     "event_name": row["event_name"], "service": row["service"],
                     **{key: json.loads(row[key]) for key in
                        ("attributes", "body", "resource_attributes")}})
    progress = [dict(row) for row in db.execute("""SELECT time_ns,substr(stage,1,256) AS stage,
        substr(message,1,1024) AS message,percent,substr(session_id,1,512) AS session_id,
        substr(service,1,128) AS service
        FROM progress WHERE trace_id=? ORDER BY time_ns DESC,id DESC LIMIT ?""", (trace_id, EVIDENCE_LIMIT))]
    if db.execute("""SELECT 1 FROM progress WHERE trace_id=? AND (
        length(stage)>256 OR length(message)>1024 OR length(session_id)>512 OR length(service)>128)
        LIMIT 1""", (trace_id,)).fetchone():
        payload_complete = False
    for log in logs:
        event = _progress_from_log(log)
        if event is not None:
            progress.append(event)
    progress.sort(key=lambda item: item["time_ns"])
    outcome_row = db.execute("""SELECT trace_id,substr(task_id,1,512) AS task_id,passed,
        CASE WHEN length(CAST(scores AS BLOB))<=65536 THEN scores ELSE '[]' END AS scores,
        substr(error,1,1024) AS error,substr(provider_model,1,128) AS provider_model,
        substr(source,1,128) AS source,
        CASE WHEN length(CAST(details AS BLOB))<=65536 THEN details ELSE '{}' END AS details,
        ingested_ns,(length(CAST(scores AS BLOB))<=65536 AND length(CAST(details AS BLOB))<=65536
            AND length(task_id)<=512 AND length(error)<=1024 AND length(provider_model)<=128) AS payload_complete
        FROM outcomes WHERE trace_id=?""", (trace_id,)).fetchone()
    if outcome_row:
        payload_complete = payload_complete and bool(outcome_row["payload_complete"])
    from .core import Store
    outcome = Store._outcome_row(outcome_row) if outcome_row else None
    timeline = progress or [{"time_ns": log["time_unix_nano"]} for log in reversed(logs)]
    summary = summarize(spans) if spans else summarize_progress(trace_id, timeline) if timeline else None
    if spans:
        # Use the actual root, never an accidental root of the evidence window.
        summary.update({key: value for key, value in summarize([span_record(root)]).items()
                        if key in {"name", "task", "waiting_for", "project", "outcome", "steps", "summary"}})
        aggregate_spans(db, trace_id, summary)
    elif summary:
        bounds = db.execute("""SELECT MIN(time_ns),MAX(time_ns) FROM (
            SELECT time_ns FROM progress WHERE trace_id=? UNION ALL
            SELECT time_ns FROM logs WHERE trace_id=?)""", (trace_id, trace_id)).fetchone()
        summary.update(start_unix_nano=bounds[0], end_unix_nano=bounds[1],
                       duration_ms=round((bounds[1] - bounds[0]) / 1e6, 3))
        services = db.execute("""SELECT substr(service,1,128),length(service)<=128 FROM (
            SELECT service FROM logs WHERE trace_id=? UNION
            SELECT service FROM progress WHERE trace_id=? AND service IS NOT NULL)
            ORDER BY service LIMIT ?""", (trace_id, trace_id, GROUP_LIMIT + 1)).fetchall()
        summary["services"] = [r[0] for r in services[:GROUP_LIMIT]]
        summary["services_complete"] = len(services) <= GROUP_LIMIT and all(r[1] for r in services)
    if outcome:
        summary = apply_outcome(summary or summarize_progress(trace_id, [{"time_ns": outcome["ingested_ns"]}]), outcome)
    if summary is None:
        return None
    lifecycle = db.execute('''SELECT json_extract(attributes,'$."observatory.source.event_name"')
        FROM logs WHERE trace_id=? AND json_extract(attributes,'$."observatory.source.event_name"')
        IN ('gemini_cli.agent.start','gemini_cli.agent.finish') ORDER BY time_ns DESC,digest DESC LIMIT 1''',
                           (trace_id,)).fetchone()
    if lifecycle and summary["status"] not in {"ERROR", "FAILED", "INCOMPLETE"}:
        state = "RUNNING" if lifecycle[0] == "gemini_cli.agent.start" else "OK"
        summary.update(status=state, active=state == "RUNNING")
    summary["session_id"] = session_for_trace(db, trace_id)
    summary["session_complete"] = True
    activity = _activity_for(spans, logs)
    summary["activity_terms"] = sorted({str(value)[:200] for item in activity
        for value in (item["domain"], item["action"], item["subject"], item["actor"]) if value})
    search_keys = ("tool.name", "tool.type", "gen_ai.tool.name", "gen_ai.tool.type", "exception.type",
                   "error.type", "gen_ai.provider.name", "gen_ai.operation.name", "openai_agents.workflow",
                   "mcp.method.name", "mcp.server.name", "db.system", "http.request.method")
    summary["span_terms"] = sorted({str(value)[:200] for span in spans
        for value in (span["name"], *(span["attributes"].get(key) for key in search_keys))
        if isinstance(value, (str, int, float)) and str(value).strip()})
    summary["log_terms"] = sorted({str(value)[:200] for log in logs
        for value in (log["event_name"], log["severity_text"]) if value})
    counts = {table: db.execute(f"SELECT COUNT(*) FROM {table} WHERE trace_id=?", (trace_id,)).fetchone()[0]
              for table in ("spans", "logs", "progress")}
    summary["evidence_window"] = {"limit": EVIDENCE_LIMIT, "total_spans": counts["spans"],
        "record_bytes": RECORD_BYTES, "window_bytes_per_signal": WINDOW_BYTES,
        "total_logs": counts["logs"], "total_progress": counts["progress"],
        "spans_returned": len(spans), "logs_returned": len(logs),
        "complete": payload_complete and counts["spans"] <= len(spans) and counts["logs"] <= len(logs)
                    and counts["progress"] <= EVIDENCE_LIMIT}
    summary["findings_complete"] = summary["evidence_window"]["complete"]
    summary["search_terms_complete"] = summary["evidence_window"]["complete"]
    timestamp = db.execute("SELECT last_ns FROM trace_activity WHERE trace_id=?", (trace_id,)).fetchone()
    timestamp = timestamp[0] if timestamp else summary["end_unix_nano"]
    findings = analyze(spans, logs, outcome)
    for finding in findings:
        finding["evidence_complete"] = summary["evidence_window"]["complete"]
        if not finding["evidence_complete"]:
            finding["detail"] += " (Bounded evidence window; not a whole-trace finding count.)"
    return summary, activity, findings, timestamp
