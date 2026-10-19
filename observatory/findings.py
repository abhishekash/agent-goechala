"""Deterministic, evidence-linked observations over captured agent activity."""
from __future__ import annotations

import hashlib
import json
import re


_SENSITIVE_FIELD = re.compile(
    r"(?:^|[._-])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|passwd|private[_-]?key|authorization|"
    r"credential|secret|bearer)(?:$|[._-])", re.IGNORECASE)
_TOKEN_FIELD = re.compile(r"(?:^|[.-])(?:auth[_-]?)?token(?:$|[.-])", re.IGNORECASE)
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"password|passwd|private[_-]?key|authorization|credential|secret|bearer)"
    r"(\s*[:=]\s*)([\"']?)([^\s\"'`,;]{4,})")
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}={0,}")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE)
_TOKEN_PATTERNS = (
    ("OpenAI or compatible", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("Slack", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)


def _safe_text(value):
    """Remove recognizable credential material before it enters a finding."""
    text = str(value)
    text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    text = _BEARER_VALUE.sub("Bearer [REDACTED]", text)
    text = _ASSIGNED_SECRET.sub(lambda match: match.group(1) + match.group(2) + "[REDACTED]", text)
    for label, pattern in _TOKEN_PATTERNS:
        text = pattern.sub(f"[REDACTED {label} token]", text)
    return text


def _credential_hits(value, field_path=""):
    """Return safe field/provider labels; never return the matched values."""
    hits = set()
    if isinstance(value, dict):
        for key, child in value.items():
            key = str(key)
            path = f"{field_path}.{key}" if field_path else key
            if (_SENSITIVE_FIELD.search(key) or _TOKEN_FIELD.search(key)) and child not in (None, "", False):
                hits.add(("sensitive field", path[:160]))
            hits.update(_credential_hits(child, path))
    elif isinstance(value, (list, tuple)):
        for child in value[:100]:
            hits.update(_credential_hits(child, field_path))
    elif isinstance(value, str):
        if _PRIVATE_KEY.search(value):
            hits.add(("private key material", field_path[:160] or "captured text"))
        if _ASSIGNED_SECRET.search(value):
            hits.add(("credential assignment", field_path[:160] or "captured text"))
        if _BEARER_VALUE.search(value):
            hits.add(("bearer token", field_path[:160] or "captured text"))
        for label, pattern in _TOKEN_PATTERNS:
            if pattern.search(value):
                hits.add((label + " token pattern", field_path[:160] or "captured text"))
    return hits


def _finding(trace_id, kind, title, severity, evidence, detail):
    source = ",".join(sorted(f"{e['type']}:{e['id']}" for e in evidence))
    digest = hashlib.sha256(f"{trace_id}:{kind}:{source}".encode()).hexdigest()[:16]
    return {"id": digest, "trace_id": trace_id, "kind": kind,
            "title": _safe_text(title), "severity": severity,
            "detail": _safe_text(detail), "evidence": evidence}


def _blocked_activity(attrs):
    result = str(attrs.get("agent.activity.result") or attrs.get("activity.result") or "").lower()
    return result in {"blocked", "denied", "policy_denied"}


def mcp_result_error(attrs):
    """Return safe text when a structured MCP tool result reports an error."""
    attrs = attrs if isinstance(attrs, dict) else {}
    if any(attrs.get(key) is True for key in ("mcp.tool.is_error", "mcp.is_error")):
        return "MCP tool response marked as an error."
    for key in ("gcp.vertex.agent.tool_response", "gen_ai.tool.call.result",
                "tool.result", "output.value"):
        value = attrs.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
        if not isinstance(value, dict) or not (
            value.get("isError") is True or value.get("is_error") is True
        ):
            continue
        content = value.get("content")
        if isinstance(content, list):
            messages = [str(item["text"]) for item in content
                        if isinstance(item, dict) and isinstance(item.get("text"), str)]
            if messages:
                return _safe_text("; ".join(messages)[:500])
        return "MCP tool response marked as an error."
    return None


def _activity_detail(attrs):
    domain = attrs.get("agent.activity.domain") or attrs.get("activity.domain") or "activity"
    action = attrs.get("agent.activity.action") or attrs.get("activity.action") or "action"
    subject = (attrs.get("agent.activity.subject") or attrs.get("agent.activity.target") or
               attrs.get("activity.subject") or attrs.get("activity.target"))
    return f"{domain}: {action}" + (f" · {subject}" if subject else "")


def analyze(spans, logs, outcome=None):
    if not spans and not logs and not outcome:
        return []
    trace_id = spans[0]["trace_id"] if spans else logs[0]["trace_id"] if logs else outcome["trace_id"]
    findings = []
    failures = {}
    credential_hits = set()
    credential_sources = {}
    for span in spans:
        attrs = span["attributes"]
        hits = _credential_hits(attrs)
        for event in span.get("events", []):
            if isinstance(event, dict):
                hits.update(_credential_hits(event.get("attributes", {}), "event." + str(event.get("name") or "event")))
        if hits:
            credential_hits.update(label for label, _ in hits)
            credential_sources.setdefault(("span", span["span_id"]), set()).update(field for _, field in hits)
        reason = attrs.get("tool.error_message") or attrs.get("tool.error")
        failed = span["status"] == "ERROR" or attrs.get("tool.result_status") == "error" or bool(attrs.get("tool.error"))
        if failed:
            title = f"Error in {attrs.get('tool.name') or span['name']}"
            findings.append(_finding(trace_id, "span_error", title, "high",
                                     [{"type": "span", "id": span["span_id"]}],
                                     str(reason or "Span status is ERROR")[:500]))
            if attrs.get("tool.name"):
                key = (str(attrs["tool.name"]), str(reason or attrs.get("tool.error") or "error"))
                failures.setdefault(key, []).append(span["span_id"])
        mcp_error = mcp_result_error(attrs)
        if mcp_error and not failed:
            tool_name = attrs.get("gen_ai.tool.name") or attrs.get("mcp.tool.name") or span["name"]
            findings.append(_finding(trace_id, "mcp_tool_error",
                                     f"MCP tool returned an error: {tool_name}", "medium",
                                     [{"type": "span", "id": span["span_id"]}], mcp_error))
        if span["name"] == "agent.run":
            stopped = attrs.get("agent.stopped_reason")
            if stopped and stopped != "completed":
                findings.append(_finding(trace_id, "incomplete_run", "Run stopped before completion", "medium",
                                         [{"type": "span", "id": span["span_id"]}],
                                         f"Recorded stop reason: {stopped}"))
        blocked = [_activity_detail(event.get("attributes", {}))
                   for event in span["events"]
                   if isinstance(event, dict) and event.get("name") == "agent.activity" and
                   isinstance(event.get("attributes"), dict) and
                   _blocked_activity(event["attributes"])]
        if blocked:
            findings.append(_finding(trace_id, "blocked_activity",
                                     f"Blocked agent activity ({len(blocked)})", "medium",
                                     [{"type": "span", "id": span["span_id"]}],
                                     "; ".join(blocked[:5])[:500]))
    for (tool, reason), ids in failures.items():
        if len(ids) >= 2:
            findings.append(_finding(trace_id, "repeated_tool_error", f"Repeated errors from {tool}", "medium",
                                     [{"type": "span", "id": value} for value in ids],
                                     f"{len(ids)} failed calls recorded with the same error: {reason[:250]}"))
    for log in logs:
        hits = _credential_hits(log.get("attributes", {}))
        hits.update(_credential_hits(log.get("body")))
        if hits:
            credential_hits.update(label for label, _ in hits)
            credential_sources.setdefault(("log", log["id"]), set()).update(field for _, field in hits)
        if log["severity_number"] >= 17:
            findings.append(_finding(trace_id, "error_log", "Error log recorded", "medium",
                                     [{"type": "log", "id": log["id"]}],
                                     str(log["body"])[:500]))
        if log.get("event_name") == "agent.activity" and isinstance(log.get("attributes"), dict) and \
                _blocked_activity(log["attributes"]):
            findings.append(_finding(trace_id, "blocked_activity", "Blocked agent activity", "medium",
                                     [{"type": "log", "id": log["id"]}],
                                     _activity_detail(log["attributes"])[:500]))
    if outcome and not outcome["passed"]:
        failed = [str(score.get("scorer") or "assertion") for score in outcome["scores"]
                  if score.get("passed") is False]
        detail = ("Failed checks: " + ", ".join(failed)) if failed else str(outcome.get("error") or "Evaluation did not pass")
        findings.append(_finding(trace_id, "failed_outcome", "Task outcome failed", "high",
                                 [{"type": "outcome", "id": trace_id}], detail[:500]))
    if outcome:
        hits = _credential_hits(outcome)
        if hits:
            credential_hits.update(label for label, _ in hits)
            credential_sources.setdefault(("outcome", outcome["trace_id"]), set()).update(
                field for _, field in hits)
    if credential_sources:
        evidence = [{"type": source_type, "id": source_id}
                    for source_type, source_id in sorted(credential_sources)]
        fields = sorted({field for values in credential_sources.values() for field in values})
        labels = ", ".join(sorted(credential_hits))
        field_summary = ", ".join(fields[:8]) or "captured content"
        if len(fields) > 8:
            field_summary += f" and {len(fields) - 8} more fields"
        severity = "high" if any(label == "private key material" or
                                  label.endswith("token pattern") for label in credential_hits) else "medium"
        findings.append(_finding(
            trace_id, "credential_exposure", "Possible credential material captured", severity,
            evidence, f"Detected {labels} in {field_summary}. Values are omitted; inspect the linked source."))
    return findings
