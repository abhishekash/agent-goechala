"""Bounded, redacted activity extracted from native agent session records."""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from .findings import _safe_text, _SENSITIVE_FIELD, _TOKEN_FIELD

CONTENT_LIMIT = 8000


def safe(value, limit=CONTENT_LIMIT):
    if isinstance(value, dict):
        value = {str(key): "[REDACTED]" if _SENSITIVE_FIELD.search(str(key)) or _TOKEN_FIELD.search(str(key))
                 else "[opaque payload omitted]" if str(key) in {"encrypted_content", "signature", "raw_content"} or str(key) == "data" and isinstance(child, str) and len(child) > 1000
                 else safe(child, 2000) for key, child in list(value.items())[:60]}
        value = json.dumps(value, ensure_ascii=False, indent=2)
    elif isinstance(value, list):
        value = json.dumps([safe(child, 2000) for child in value[:60]], ensure_ascii=False, indent=2)
    text = _safe_text(value)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = "".join(char for char in text if char in "\n\t" or ord(char) >= 32 and ord(char) != 127)
    return text[:limit] + ("\n[truncated]" if len(text) > limit else "")


def plaintext(content):
    if isinstance(content, str):
        return safe(content)
    if not isinstance(content, list):
        return ""
    return safe("\n".join(str(block.get("text") or "") for block in content
                         if isinstance(block, dict) and block.get("type") in {"text", "input_text", "output_text"}))


def emit(session, row, stamp, domain, action, subject, detail="", result="observed", extra=None):
    if not stamp:
        return None
    from .local_agents import identity
    attrs = {"session.id": session.session_id, "observatory.project": session.project,
             "observatory.data_source": "local-session-log", "agent.activity.domain": domain,
             "agent.activity.action": action, "agent.activity.subject": safe(subject, 500),
             "agent.activity.result": result, "agent.activity.actor": session.runtime,
             "observatory.record_id": identity(session.source_id, row.get("ordinal") or row.get("uuid") or stamp,
                                                domain, action, str(subject)[:500]), **(extra or {})}
    payload = row.get("payload") or {}
    item = payload.get("item") or {} if isinstance(payload, dict) else {}
    span_id = None
    if isinstance(item, dict) and item.get("type") in {"CommandExecution", "FileChange", "McpToolCall", "Extension"}:
        span_id = identity(session.source_id, ("tool", item.get("id") or row.get("ordinal") or stamp), size=16)
    return {"trace_id": identity(session.source_id, session.turn), "span_id": span_id,
            "time_unix_nano": stamp, "service": session.service,
            "severity_number": 17 if result in {"error", "failed", "denied"} else 9,
            "severity_text": "ERROR" if result in {"error", "failed", "denied"} else "INFO",
            "event_name": "agent.activity", "body": safe(detail) if session.capture_details else "",
            "attributes": attrs, "resource_attributes": {"observatory.source_id": session.source_id,
                "observatory.capture": "local-session-log"}, "scope_name": "observatory.local_agents"}


def duration(item):
    value = item.get("duration")
    if isinstance(value, dict):
        try:
            return float(value.get("secs", 0)) * 1000 + float(value.get("nanos", 0))/1e6
        except (ValueError, TypeError):
            pass
    return None


def codex_activity(session, row, stamp):
    payload = row.get("payload") or {}
    if not isinstance(payload, dict):
        return []
    kind = row.get("type")
    event = payload.get("type")
    out = []
    if kind == "token_usage_record":
        usage = payload.get("usage") or {}
        out.append(emit(session, row, stamp, "model", "usage", session.model or "Model not reported", usage,
                        "reported", {"gen_ai.request.model": str(session.model or "unknown")}))
    elif kind == "response_item" and event in {"function_call", "custom_tool_call"}:
        name = str(payload.get("name") or "Tool")
        args = payload.get("arguments") or payload.get("input") or ""
        try:
            parsed = json.loads(args) if isinstance(args, str) else args
        except (ValueError, TypeError):
            parsed = {}
        session.tool_inputs[str(payload.get("call_id") or payload.get("id"))] = (name, safe(args))
        subject = name
        domain = "tool"
        if isinstance(parsed, dict):
            command = parsed.get("cmd") or parsed.get("command")
            if command:
                domain, subject = "shell", str(command)
            elif parsed.get("url"):
                domain, subject = "browser", str(parsed["url"])
            elif parsed.get("path"):
                domain, subject = "file", str(parsed["path"])
        out.append(emit(session, row, stamp, domain, "call", subject if session.capture_details else name,
                        args, "started", {"tool.name": name[:128]}))
    elif kind == "response_item" and event in {"function_call_output", "custom_tool_call_output"}:
        key = str(payload.get("call_id") or payload.get("id"))
        name, _ = session.tool_inputs.get(key, ("Tool", ""))
        out.append(emit(session, row, stamp, "tool", "result", name, payload.get("output") or "", "observed"))
    elif kind == "event_msg" and event in {"task_started", "task_complete", "task_aborted"}:
        action = "started" if event == "task_started" else "completed" if event == "task_complete" else "interrupted"
        out.append(emit(session, row, stamp, "agent", action, session.project or session.runtime,
                        {key: payload[key] for key in ("duration_ms", "time_to_first_token_ms") if key in payload}, action))
    elif kind == "turn_context":
        # Only selected settings, never the system instructions or environment.
        settings = {key: payload[key] for key in ("model", "effort", "approval_policy", "sandbox_policy") if key in payload}
        out.append(emit(session, row, stamp, "context", "settings", session.model or "Agent settings", settings))
    elif kind == "event_msg" and event == "item_completed":
        item = payload.get("item") or {}
        if not isinstance(item, dict):
            return []
        item_type = item.get("type")
        extra = {"observatory.item_type": item_type}
        elapsed = duration(item)
        if elapsed is not None:
            extra["duration_ms"] = elapsed
        failed = str(item.get("status", "")).lower() in {"failed", "error", "declined", "denied"}
        result = "error" if failed else "success"
        if item_type == "CommandExecution":
            command = item.get("command") or []
            command = shlex.join(map(str, command)) if isinstance(command, list) else str(command)
            code = item.get("exit_code")
            if isinstance(code, int):
                extra["process.exit.code"] = code
                if code:
                    result = "error"
            out.append(emit(session, row, stamp, "shell", "execute", command if session.capture_details else "Shell command",
                {"command": command, "cwd": item.get("cwd"), "exit_code": code, "output": item.get("aggregated_output", "")}, result, extra))
        elif item_type == "FileChange":
            changes = item.get("changes") or {}
            if isinstance(changes, dict):
                for path, change in list(changes.items())[:60]:
                    operation = change.get("type") or change.get("kind") or "change" if isinstance(change, dict) else "change"
                    if isinstance(operation, dict):
                        operation = next(iter(operation), "change")
                    operation = str(operation)
                    out.append(emit(session, row, stamp, "file", operation[:40], path if session.capture_details else Path(path).name,
                                    change, result, extra))
        elif item_type == "Extension":
            action = item.get("action") or {}
            name = str(action.get("type") or item.get("kind") or "activity") if isinstance(action, dict) else str(item.get("kind") or "activity")
            subject = action.get("url") or item.get("query") or name if isinstance(action, dict) else name
            out.append(emit(session, row, stamp, "browser", name, subject if session.capture_details else name,
                {"action": action, "query": item.get("query"), "results": item.get("results")}, result, extra))
        elif item_type == "McpToolCall":
            name = str(item.get("tool") or "MCP tool")
            extra["mcp.server.name"] = str(item.get("server") or "")[:128]
            extra["mcp.tool.name"] = name
            extra["mcp.method.name"] = "tools/call"
            details = {"arguments": item.get("arguments"), "result": item.get("result")}
            if isinstance(item.get("result"), dict) and item["result"].get("isError"):
                result = "error"
            out.append(emit(session, row, stamp, "mcp", name, str(item.get("server") or "MCP"), details, result, extra))
        elif item_type in {"UserMessage", "AgentMessage"}:
            text = "" if session.internal else plaintext(item.get("content"))
            role = "user" if item_type == "UserMessage" else "assistant"
            if role == "user" and text and session.capture_details:
                session.tasks[session.turn] = text[:4000]
            out.append(emit(session, row, stamp, "message", role,
                "Approval review" if session.internal else text.splitlines()[0][:300] if text and session.capture_details else role + " message", text,
                "observed", {"message.role": role, "message.phase": item.get("phase") or ""}))
        elif item_type in {"Reasoning", "ContextCompaction"}:
            out.append(emit(session, row, stamp, "agent", "processing" if item_type == "Reasoning" else "compaction",
                            "Model processing" if item_type == "Reasoning" else "Context compacted"))
    return [entry for entry in out if entry]


def claude_activity(session, row, stamp):
    message = row.get("message") or {}
    if not isinstance(message, dict):
        return []
    content = message.get("content") or []
    blocks = content if isinstance(content, list) else []
    out = []
    if row.get("type") == "assistant" and isinstance(message.get("usage"), dict):
        out.append(emit(session, row, stamp, "model", "usage", session.model or "Model not reported", message["usage"], "reported"))
    text = plaintext(content)
    if text and row.get("type") in {"assistant", "user"}:
        role = row["type"]
        if role == "user" and session.capture_details:
            session.tasks[session.turn] = text[:4000]
        out.append(emit(session, row, stamp, "message", role, text.splitlines()[0][:300] if session.capture_details else role+" message", text))
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            name, args = str(block.get("name") or "Tool"), block.get("input") or {}
            domain = "shell" if name.lower() in {"bash", "shell", "terminal"} else "file" if name.lower() in {"read", "write", "edit", "glob", "grep", "multiedit"} else "browser" if name.lower() in {"webfetch", "websearch"} else "tool"
            subject = args.get("command") or args.get("file_path") or args.get("path") or args.get("url") or args.get("query") or name if isinstance(args, dict) else name
            session.tool_inputs[str(block.get("id"))] = (name, safe(subject, 500))
            out.append(emit(session, row, stamp, domain, name, subject if session.capture_details else name, args, "started", {"tool.name": name}))
        elif block.get("type") == "tool_result":
            name, subject = session.tool_inputs.get(str(block.get("tool_use_id")), ("Tool", "Tool result"))
            out.append(emit(session, row, stamp, "tool", name+" result", subject if session.capture_details else name,
                block.get("content") or "", "error" if block.get("is_error") else "success"))
        elif block.get("type") == "thinking":
            out.append(emit(session, row, stamp, "agent", "processing", "Model processing"))
    if row.get("type") == "system":
        out.append(emit(session, row, stamp, "context", str(row.get("subtype") or "event"), "Session event",
                        {key:row[key] for key in ("durationMs", "messageCount", "isMeta") if key in row}))
    return [entry for entry in out if entry]
