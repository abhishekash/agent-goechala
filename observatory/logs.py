"""Normalize OTLP log records from JSON and HTTP/protobuf requests."""
from __future__ import annotations

import time

from .core import TRACE_ID, SPAN_ID, _attrs, _value
from .otlp_proto import _fields, _nested, _attributes, _any_value, _text


def _optional_id(value, pattern):
    if isinstance(value, str) and pattern.fullmatch(value) and any(c != "0" for c in value):
        return value.lower()
    return None


def _token_count(*values):
    total = 0
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            total += max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            continue
    return total


def _normalize_runtime_event(event_name, attributes, body=None):
    """Map well-known runtime log events onto the shared activity vocabulary."""
    attrs = dict(attributes)
    if not isinstance(event_name, str):
        return event_name, attrs

    if event_name.startswith("cursor."):
        return _normalize_cursor_event(event_name, attrs, body)
    if not event_name.startswith("gemini_cli."):
        return event_name, attrs

    event = event_name.removeprefix("gemini_cli.")
    attrs["observatory.source.event_name"] = event_name
    attrs.setdefault("agent.activity.actor", "gemini-cli")
    if event == "tool_call":
        tool = attrs.get("function_name") or attrs.get("tool_name") or "Tool"
        is_mcp = str(attrs.get("tool_type", "")).casefold() == "mcp"
        attrs.update({"tool.name": tool, "agent.activity.action": "tool_call",
                      "agent.activity.domain": "mcp" if is_mcp else "tool",
                      "agent.activity.subject": attrs.get("mcp_server_name") if is_mcp else tool,
                      "agent.activity.result": "success" if attrs.get("success") is True else
                          "error" if attrs.get("success") is False else
                          "denied" if attrs.get("decision") == "reject" else "observed"})
        if is_mcp:
            attrs.update({"mcp.server.name": attrs.get("mcp_server_name") or "Not reported",
                          "mcp.tool.name": tool, "mcp.method.name": "tools/call",
                          "rpc.system": "mcp"})
        if "function_args" in attrs:
            attrs.setdefault("gen_ai.tool.call.arguments", attrs["function_args"])
    elif event == "file_operation":
        operation = str(attrs.get("operation") or "file operation")
        attrs.update({"agent.activity.domain": "file", "agent.activity.action": operation,
                      "agent.activity.subject": attrs.get("tool_name") or "file",
                      "agent.activity.result": "observed"})
    elif event in {"agent.start", "agent.finish"}:
        finished = event.endswith("finish")
        attrs.update({"agent.activity.domain": "agent",
                      "agent.activity.action": "run_finished" if finished else "run_started",
                      "agent.activity.subject": attrs.get("agent_name") or "Gemini CLI",
                      "agent.activity.result": "completed" if finished else "started"})
    elif event in {"api_response", "api_error"}:
        failed = event == "api_error" or str(attrs.get("status_code", "")).startswith(("4", "5"))
        attrs.update({"agent.activity.domain": "model",
                      "agent.activity.action": "request_failed" if failed else "response",
                      "agent.activity.subject": attrs.get("model") or attrs.get("model_name") or "Gemini model",
                      "agent.activity.result": "error" if failed else "success"})
    elif event == "user_prompt":
        attrs.update({"agent.activity.domain": "message", "agent.activity.action": "prompt_submitted",
                      "agent.activity.subject": "user prompt", "agent.activity.result": "observed"})
    else:
        # Keep other named Gemini events discoverable in Activity without
        # pretending they are tool calls or successful agent actions.
        attrs.update({"agent.activity.domain": "runtime", "agent.activity.action": event,
                      "agent.activity.subject": attrs.get("agent_name") or "Gemini CLI",
                      "agent.activity.result": "observed"})
    return "agent.activity", attrs


def _normalize_cursor_event(event_name, attrs, body=None):
    """Map Cursor Enterprise OTLP log events without inventing trace context."""
    event = event_name.removeprefix("cursor.")
    attrs["observatory.source.event_name"] = event_name
    attrs.setdefault("agent.activity.actor", "cursor")

    def activity(domain, action, subject, result="observed"):
        attrs.update({"agent.activity.domain": domain, "agent.activity.action": action,
                      "agent.activity.subject": subject or "Cursor", "agent.activity.result": result})

    model = attrs.get("cursor.model.name") or attrs.get("gen_ai.request.model")
    if event == "api.request":
        attrs.setdefault("gen_ai.request.model", model)
        if "cursor.api.request.input_tokens" in attrs:
            # Cursor reports uncached input separately from cache read/create
            # counts. GenAI input usage and Goechala's headline total count
            # all input token classes, while the original fields remain intact.
            attrs["gen_ai.usage.input_tokens"] = _token_count(
                attrs.get("cursor.api.request.input_tokens"),
                attrs.get("cursor.api.request.cache_read_tokens"),
                attrs.get("cursor.api.request.cache_creation_tokens"))
        for source, target in (("cursor.api.request.input_tokens", "gen_ai.usage.input_tokens"),
                               ("cursor.api.request.output_tokens", "gen_ai.usage.output_tokens"),
                               ("cursor.api.request.cache_read_tokens", "gen_ai.usage.cache_read.input_tokens"),
                               ("cursor.api.request.cache_creation_tokens", "gen_ai.usage.cache_write.input_tokens"),
                               ("cursor.api.request.reasoning_tokens", "gen_ai.usage.reasoning_tokens")):
            if source in attrs:
                attrs.setdefault(target, attrs[source])
        activity("model", "request", model or "Cursor model", "success")
    elif event == "api.error":
        attrs.setdefault("gen_ai.request.model", model)
        activity("model", "request", model or "Cursor model", "error")
    elif event == "api.correction":
        activity("billing", "usage_correction", attrs.get("cursor.api.correction.kind"), "warning")
    elif event == "skill.activated":
        activity("skill", "activate", attrs.get("cursor.skill.name"))
    elif event == "hook.execution_complete":
        outcome = str(attrs.get("cursor.hook.outcome") or "unknown").casefold()
        result = "success" if outcome == "success" else "blocked" if outcome == "blocked" else "error"
        attrs.setdefault("duration_ms", attrs.get("cursor.hook.duration_ms"))
        activity("hook", str(attrs.get("cursor.hook.type") or "execute"), attrs.get("cursor.hook.name"), result)
    elif event == "plugin.installed":
        activity("plugin", "install", attrs.get("cursor.plugin.name"))
    elif event == "grok_bot.mcp_tool_call":
        tool = attrs.get("cursor.tool.name") or "MCP tool"
        server = attrs.get("cursor.mcp.server.name") or "Not reported"
        status = str(attrs.get("cursor.tool.status") or "").casefold()
        result = "error" if status == "failure" else "success" if status == "success" else "observed"
        attrs.update({"mcp.server.name": server, "mcp.tool.name": tool,
                      "mcp.method.name": "tools/call", "rpc.system": "mcp",
                      "tool.name": tool, "tool.type": "mcp"})
        call_id = attrs.get("cursor.grok_bot.tool_call.id")
        if call_id:
            attrs["observatory.mcp.call_id"] = str(call_id)
        attrs.setdefault("duration_ms", attrs.get("cursor.grok_bot.mcp.duration_ms"))
        activity("mcp", "tool_call", tool, result)
    elif event == "conversation.tool_io":
        tool = attrs.get("cursor.tool.name") or "MCP tool"
        server = attrs.get("cursor.mcp.server.name") or "Not reported"
        direction = str(attrs.get("cursor.conversation.tool_io.direction") or "").casefold()
        status = str(attrs.get("cursor.tool.status") or "").casefold()
        result = "error" if status == "failure" else "success" if status == "success" else "observed"
        attrs.update({"mcp.server.name": server, "mcp.tool.name": tool,
                      "mcp.method.name": "tools/call", "rpc.system": "mcp",
                      "tool.name": tool, "tool.type": "mcp",
                      "observatory.mcp.io.direction": direction})
        call_id = attrs.get("cursor.grok_bot.tool_call.id")
        if call_id:
            attrs["observatory.mcp.call_id"] = str(call_id)
        activity("mcp", "tool_io_" + direction if direction else "tool_io", tool, result)
    elif event in {"grok_bot.shell_command", "grok_bot.browser_navigation",
                   "grok_bot.computer_use_session", "grok_bot.file_transfer",
                   "grok_bot.message_delivery", "grok_bot.delegation", "grok_bot.routine_run",
                   "grok_bot.guardrail", "grok_bot.tool_decision", "grok_bot.tool_result"}:
        subject = (attrs.get("cursor.grok_bot.shell.command") or
                   attrs.get("cursor.grok_bot.browser.url") or attrs.get("cursor.tool.name") or event)
        outcome = str(attrs.get("cursor.grok_bot.routine_run.outcome") or "").casefold()
        result = (("completed" if outcome == "success" else "cancelled" if outcome == "cancelled" else "error")
                  if event == "grok_bot.routine_run" and outcome else
                  "completed" if event == "grok_bot.routine_run" else
                  "error" if str(attrs.get("cursor.tool.status") or "").casefold() == "failure" else
                  "observed")
        duration_keys = {
            "grok_bot.shell_command": "cursor.grok_bot.shell.duration_ms",
            "grok_bot.computer_use_session": "cursor.grok_bot.computer_use.duration_ms",
            "grok_bot.tool_result": "cursor.grok_bot.tool_result.duration_ms",
            "grok_bot.routine_run": "cursor.grok_bot.routine_run.duration_ms",
            "grok_bot.guardrail": "cursor.grok_bot.guardrail.duration_ms",
        }
        if duration_keys.get(event) in attrs:
            attrs.setdefault("duration_ms", attrs[duration_keys[event]])
        if event == "grok_bot.shell_command":
            if attrs.get("cursor.grok_bot.shell.allowed") is False:
                result = "blocked"
            elif attrs.get("cursor.grok_bot.shell.exit_code") not in (None, 0):
                result = "error"
        elif event == "grok_bot.tool_result":
            outcome = str(attrs.get("cursor.grok_bot.tool_result.outcome") or "").casefold()
            result = {"success": "success", "error": "error", "denied": "blocked",
                      "cancelled": "cancelled"}.get(outcome, result)
        elif event == "grok_bot.tool_decision":
            outcome = str(attrs.get("cursor.grok_bot.decision.outcome") or "").casefold()
            result = {"allowed": "success", "denied": "blocked", "held": "waiting",
                      "timed_out": "error"}.get(outcome, result)
            if outcome == "held":
                attrs["agent.waiting_for"] = "Approval" + (
                    " · " + str(attrs["cursor.tool.name"]) if attrs.get("cursor.tool.name") else "")
        elif event == "grok_bot.file_transfer":
            outcome = str(attrs.get("cursor.grok_bot.file.outcome") or "").casefold()
            result = {"success": "success", "error": "error", "denied": "blocked"}.get(outcome, result)
            subject = (str(attrs.get("cursor.grok_bot.file.direction") or "file transfer") + " · " +
                       str(attrs.get("cursor.grok_bot.file.target") or "target unknown"))
        elif event == "grok_bot.message_delivery":
            outcome = str(attrs.get("cursor.grok_bot.message_delivery.result") or "").casefold()
            result = {"sent": "success", "failed": "error", "held": "waiting"}.get(outcome, result)
            subject = attrs.get("cursor.grok_bot.message_delivery.destination_type") or "message"
            if outcome == "held":
                attrs["agent.waiting_for"] = "Message approval"
        if event == "grok_bot.guardrail":
            kind = str(attrs.get("cursor.grok_bot.guardrail.kind") or "")
            resolution = str(attrs.get("cursor.grok_bot.guardrail.resolution") or "")
            if kind == "tool_escalation":
                attrs["agent.waiting_for"] = "Approval" + (
                    " · " + str(attrs["cursor.tool.name"]) if attrs.get("cursor.tool.name") else "")
                result = "waiting"
            elif kind in {"pause", "interrupted"} and resolution == "denied":
                result = "blocked"
            elif kind in {"pause", "interrupted"} and resolution == "abandoned":
                result = "cancelled"
        activity("agent", event.removeprefix("grok_bot."), subject, result)
    elif event.startswith("cloud_agent."):
        kind = next((value for key, value in attrs.items() if key.endswith(".kind") and value), "")
        status = str(kind).casefold()
        result = "error" if status in {"failed", "creation_failed"} else "completed" if status in {"completed", "merged", "closed"} else "started" if status == "started" else "observed"
        subject = attrs.get("vcs.repository.name") or attrs.get("vcs.repository.url") or event
        activity("agent", event.removeprefix("cloud_agent."), subject, result)
    elif event.startswith("conversation."):
        # Conversation content is opt-in at the Cursor destination. Keep the
        # event category and metadata; the receiver does not opt in to content.
        if event == "conversation.user_message":
            activity("message", "user", body if isinstance(body, str) and body else "Cursor conversation")
        else:
            activity("message", event.removeprefix("conversation."), "Cursor conversation")
    else:
        activity("runtime", event, "Cursor")
    return "agent.activity", attrs


def _log(raw, service, resource, scope):
    timestamp = int(raw.get("time_unix_nano") or raw.get("observed_time_unix_nano") or time.time_ns())
    if timestamp < 0:
        raise ValueError("invalid log timestamp")
    attrs = raw.get("attributes") or {}
    if not isinstance(attrs, dict):
        raise ValueError("log attributes must be an object")
    event_name = str(raw.get("event_name") or attrs.get("event.name") or "")
    event_name, attrs = _normalize_runtime_event(event_name, attrs, raw.get("body"))
    return {"time_unix_nano": timestamp,
            "trace_id": _optional_id(raw.get("trace_id"), TRACE_ID),
            "span_id": _optional_id(raw.get("span_id"), SPAN_ID),
            "service": service, "scope_name": scope,
            "resource_attributes": resource,
            "severity_number": int(raw.get("severity_number") or 0),
            "severity_text": str(raw.get("severity_text") or ""),
            "event_name": event_name,
            "body": raw.get("body"), "attributes": attrs}


def normalize_otlp_logs(body):
    result = []
    for group in body.get("resourceLogs", []):
        resource = _attrs(group.get("resource", {}).get("attributes"))
        service = str(resource.get("service.name") or "unknown")
        for scope in group.get("scopeLogs", []):
            scope_name = str(scope.get("scope", {}).get("name") or "")
            for item in scope.get("logRecords", []):
                raw = {"time_unix_nano": item.get("timeUnixNano"),
                       "observed_time_unix_nano": item.get("observedTimeUnixNano"),
                       "trace_id": item.get("traceId"), "span_id": item.get("spanId"),
                       "severity_number": item.get("severityNumber"),
                       "severity_text": item.get("severityText"),
                       "event_name": item.get("eventName"),
                       "body": _value(item.get("body", {})),
                       "attributes": _attrs(item.get("attributes"))}
                result.append(_log(raw, service, resource, scope_name))
    return result


def _record_proto(raw, service, resource, scope, depth):
    item = {"attributes": {}}
    for number, wire, value in _nested(raw, depth):
        if number == 1 and wire == 1:
            item["time_unix_nano"] = value
        elif number == 11 and wire == 1:
            item["observed_time_unix_nano"] = value
        elif number == 2 and wire == 0:
            item["severity_number"] = value
        elif number == 3 and wire == 2:
            item["severity_text"] = _text(value)
        elif number == 5 and wire == 2:
            item["body"] = _any_value(value, depth + 1)
        elif number == 6 and wire == 2:
            key, attr = _kv_log(value, depth + 1)
            if key:
                item["attributes"][key] = attr
        elif number == 9 and wire == 2:
            item["trace_id"] = value.hex()
        elif number == 10 and wire == 2:
            item["span_id"] = value.hex()
        elif number == 12 and wire == 2:
            item["event_name"] = _text(value)
    return _log(item, service, resource, scope)


def _kv_log(raw, depth):
    from .otlp_proto import _kv
    return _kv(raw, depth)


def decode_log_request(raw):
    result = []
    for number, wire, group in _fields(raw):
        if number != 1 or wire != 2:
            continue
        resource = {}
        scopes = []
        for field, kind, value in _nested(group, 1):
            if field == 1 and kind == 2:
                resource = _attributes(value, 2)
            elif field == 2 and kind == 2:
                scopes.append(value)
        service = str(resource.get("service.name") or "unknown")
        for scope in scopes:
            scope_name = ""
            records = []
            for field, kind, value in _nested(scope, 2):
                if field == 1 and kind == 2:
                    for key, typ, item in _nested(value, 3):
                        if key == 1 and typ == 2:
                            scope_name = _text(item)
                elif field == 2 and kind == 2:
                    records.append(value)
            result.extend(_record_proto(item, service, resource, scope_name, 3) for item in records)
    return result
