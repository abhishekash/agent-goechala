"""Control-center snapshots and the OpenCode live control adapter."""
from __future__ import annotations

import base64
import hmac
import json
import os
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, ProxyHandler

from .ai_summary import SummaryConfig, generate_summary, summary_evidence
from .local_activity import safe


class ControlError(ValueError):
    pass


class ControlBroker:
    """One-shot decisions, scoped to an exact runtime/session/request tuple."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}

    def register(self, runtime, session_id, kind, detail):
        if runtime not in {"Codex", "Claude Code"} or kind not in {"permission", "question"}:
            raise ControlError("unsupported control request")
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 512 or not isinstance(detail, dict):
            raise ControlError("invalid control request")
        request_id = uuid.uuid4().hex
        item = {"runtime": runtime, "session_id": session_id, "kind": kind,
                "id": request_id, "detail": detail, "created_ns": time.time_ns()}
        owner_token = uuid.uuid4().hex
        with self.lock:
            self.pending[request_id] = {"item": item, "event": threading.Event(),
                                        "decision": None, "owner_token": owner_token,
                                        "last_poll_ns": time.time_ns()}
        return {**item, "owner_token": owner_token}

    def list(self):
        with self.lock:
            now = time.time_ns()
            for request_id, entry in list(self.pending.items()):
                if entry["item"]["runtime"] == "Codex" and now-entry["last_poll_ns"] > 90_000_000_000:
                    entry["event"].set()
                    self.pending.pop(request_id)
            return [entry["item"] for entry in self.pending.values() if entry["decision"] is None]

    def reply(self, runtime, session_id, kind, request_id, decision=None, answers=None):
        with self.lock:
            entry = self.pending.get(request_id)
            if not entry or entry["decision"] is not None:
                raise ControlError("The request is no longer pending; refresh the control center")
            item = entry["item"]
            if (item["runtime"], item["session_id"], item["kind"]) != (runtime, session_id, kind):
                raise ControlError("request does not belong to this session")
            if kind == "permission":
                allowed = {"Codex": {"accept", "decline"}, "Claude Code": {"once", "reject"}}[runtime]
                if decision not in allowed:
                    raise ControlError("invalid permission decision")
                value = {"decision": decision}
            else:
                questions = item["detail"].get("questions") or []
                if not isinstance(answers, list) or len(answers) != len(questions) or not all(
                        isinstance(group, list) and group and all(isinstance(answer, str) and answer.strip()
                        and len(answer) <= 1000 for answer in group) for group in answers):
                    raise ControlError("Answer each question before replying")
                value = {"answers": answers}
            entry["decision"] = value
            entry["event"].set()
        return {"accepted": True}

    def wait(self, request_id, timeout=600):
        with self.lock:
            entry = self.pending.get(request_id)
        if not entry:
            raise ControlError("request is no longer pending")
        entry["event"].wait(timeout)
        with self.lock:
            current = self.pending.pop(request_id, None)
        return current["decision"] if current else None

    def poll(self, request_id, owner_token, timeout=20):
        with self.lock:
            entry = self.pending.get(request_id)
            if not entry or not hmac.compare_digest(str(entry["owner_token"]), str(owner_token)):
                raise ControlError("request is no longer pending")
            entry["last_poll_ns"] = time.time_ns()
        entry["event"].wait(timeout)
        with self.lock:
            current = self.pending.get(request_id)
            if not current:
                raise ControlError("request is no longer pending")
            if current["decision"] is None:
                return {"pending": True}
            self.pending.pop(request_id, None)
            return {"pending": False, **current["decision"]}

    def cancel(self, request_id, owner_token=None):
        with self.lock:
            entry = self.pending.get(request_id)
            if entry and not hmac.compare_digest(str(entry["owner_token"]), str(owner_token)):
                raise ControlError("request does not belong to this controller")
            entry = self.pending.pop(request_id, None)
            if entry:
                entry["event"].set()


def claude_hook_response(payload, decision):
    event = payload.get("hook_event_name")
    if event == "PermissionRequest":
        allow = decision and decision.get("decision") == "once"
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow" if allow else "deny",
                         **({} if allow else {"message": "Denied or timed out in Agent Observatory"})}}}
    if event == "PreToolUse" and payload.get("tool_name") == "AskUserQuestion":
        questions = (payload.get("tool_input") or {}).get("questions") or []
        if not decision:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "No answer received in Agent Observatory"}}
        answers = {question["question"]: ", ".join(group)
                   for question, group in zip(questions, decision["answers"])}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                "updatedInput": {**payload.get("tool_input", {}), "answers": answers}}}
    raise ControlError("unsupported Claude hook event")


def _text(value, limit=4000):
    return safe(value or "", limit)


class OpenCode:
    """Connect only to an explicitly configured local OpenCode server."""

    def __init__(self, url=None, password=None, username=None):
        self.url = (url if url is not None else os.environ.get("OBSERVATORY_OPENCODE_URL", "")).rstrip("/")
        parsed = urlsplit(self.url)
        if self.url and (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                         or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ControlError("OpenCode URL must be an HTTP loopback origin without credentials or a path")
        self.password = password if password is not None else os.environ.get("OBSERVATORY_OPENCODE_PASSWORD", "")
        self.username = username if username is not None else os.environ.get("OBSERVATORY_OPENCODE_USERNAME", "opencode")
        self.opener = build_opener(ProxyHandler({}))

    @property
    def enabled(self):
        return bool(self.url)

    def request(self, method, path, body=None):
        if not self.enabled:
            raise ControlError("Set OBSERVATORY_OPENCODE_URL to a local OpenCode server")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.password:
            credential = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            headers["Authorization"] = "Basic " + credential
        req = Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=3) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ControlError("OpenCode response exceeded 2 MB")
            return json.loads(raw) if raw else None
        except HTTPError as exc:
            raise ControlError(f"OpenCode returned HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise ControlError("OpenCode server is unavailable") from None
        except ControlError:
            raise
        except ValueError:
            raise ControlError("OpenCode returned invalid JSON") from None

    def snapshot(self):
        if not self.enabled:
            return {"sessions": [], "attention": [], "error": "OpenCode server not configured"}
        try:
            sessions = self.request("GET", "/session?limit=100") or []
            statuses = self.request("GET", "/session/status") or {}
            permissions = self.request("GET", "/permission") or []
            questions = self.request("GET", "/question") or []
        except ControlError as exc:
            return {"sessions": [], "attention": [], "error": str(exc)}
        if not isinstance(sessions, list) or not isinstance(statuses, dict):
            raise ControlError("OpenCode returned an unexpected session response")
        attention = []
        for kind, requests in (("permission", permissions), ("question", questions)):
            if not isinstance(requests, list):
                continue
            for item in requests:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("sessionID"), str):
                    continue
                attention.append({"runtime": "OpenCode", "kind": kind, "id": item["id"],
                                  "session_id": item["sessionID"], "detail": item})
        result = []
        for item in sessions[:100]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            session_id = item["id"]
            status = statuses.get(session_id) or {}
            status_type = status.get("type", "idle") if isinstance(status, dict) else "idle"
            updated = (item.get("time") or {}).get("updated", 0)
            pending = [request for request in attention if request["session_id"] == session_id]
            result.append({"runtime": "OpenCode", "session_id": session_id,
                           "project": _text(item.get("directory") or item.get("title") or "", 300),
                           "task": _text(item.get("title") or "", 500), "goal": "",
                           "state": "waiting" if pending else "running" if status_type in {"busy", "retry"} else "idle",
                           "waiting_for": ", ".join(request["kind"] for request in pending),
                           "updated_ns": int(updated or 0) * 1_000_000,
                           "attention": pending, "can_reply": True})
        return {"sessions": result, "attention": attention, "error": ""}

    def detail(self, session_id):
        records = self.request("GET", "/session/" + quote(session_id, safe="") + "/message?limit=100") or []
        if not isinstance(records, list):
            raise ControlError("OpenCode returned an unexpected message response")
        events = []
        for record in records:
            if not isinstance(record, dict):
                continue
            info = record.get("info") or {}
            parts = record.get("parts") or []
            if not isinstance(info, dict) or not isinstance(parts, list):
                continue
            role = info.get("role", "unknown")
            stamp = int((info.get("time") or {}).get("created") or 0) * 1_000_000
            for part in parts:
                if not isinstance(part, dict):
                    continue
                kind = part.get("type")
                if kind == "text":
                    text = _text(part.get("text"))
                    if text:
                        events.append({"time_unix_nano": stamp, "domain": "message", "action": role,
                                       "subject": text.splitlines()[0][:300], "detail": text, "result": "observed"})
                elif kind == "tool":
                    state = part.get("state") or {}
                    status = state.get("status", "unknown") if isinstance(state, dict) else "unknown"
                    events.append({"time_unix_nano": stamp, "domain": "tool", "action": _text(part.get("tool"), 120),
                                   "subject": _text((state.get("title") if isinstance(state, dict) else "") or part.get("tool"), 300),
                                   "detail": _text(state.get("error") if isinstance(state, dict) else "", 1000),
                                   "result": "error" if status == "error" else status})
            if info.get("error"):
                events.append({"time_unix_nano": stamp, "domain": "agent", "action": "error",
                               "subject": _text(info["error"], 300), "detail": _text(info["error"]), "result": "error"})
        events.sort(key=lambda event: event["time_unix_nano"])
        goal = next((event["detail"] for event in events if event["domain"] == "message" and event["action"] == "user"), "")
        return {"goal": goal, "events": events}

    def start(self, prompt):
        """Create an OpenCode session and submit its first prompt."""
        title = prompt.splitlines()[0].strip()[:120] or "Agent task"
        session = self.request("POST", "/session", {"title": title})
        session_id = session.get("id") if isinstance(session, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise ControlError("OpenCode did not return a session ID")
        self.request("POST", "/session/" + quote(session_id, safe="") + "/prompt_async",
                     {"parts": [{"type": "text", "text": prompt}]})
        return {"runtime": "OpenCode", "session_id": session_id}

    def reply(self, session_id, kind, request_id, decision=None, answers=None):
        if kind not in {"permission", "question"}:
            raise ControlError("Unsupported request type")
        current = self.request("GET", "/" + kind) or []
        request = next((item for item in current if isinstance(item, dict) and item.get("id") == request_id
                        and item.get("sessionID") == session_id), None)
        if request is None:
            raise ControlError("The request is no longer pending; refresh the control center")
        if kind == "permission":
            if decision not in {"once", "always", "reject"}:
                raise ControlError("Invalid permission decision")
            path = ("/session/" + quote(session_id, safe="") + "/permissions/"
                    + quote(request_id, safe=""))
            body = {"response": decision}
            legacy_path = "/permission/" + quote(request_id, safe="") + "/reply"
            legacy_body = {"reply": decision}
        else:
            if not isinstance(answers, list) or len(answers) != len(request.get("questions") or []):
                raise ControlError("Answer each question before replying")
            if not all(isinstance(group, list) and group and all(isinstance(value, str) and value.strip() and len(value) <= 1000
                           for value in group) for group in answers):
                raise ControlError("Question answers must be nonempty text")
            path = ("/session/" + quote(session_id, safe="") + "/question/"
                    + quote(request_id, safe="") + "/reply")
            body = {"answers": answers}
            legacy_path = "/question/" + quote(request_id, safe="") + "/reply"
            legacy_body = body
        try:
            self.request("POST", path, body)
        except ControlError as exc:
            if str(exc) != "OpenCode returned HTTP 404":
                raise
            self.request("POST", legacy_path, legacy_body)
        return {"accepted": True}


def native_sessions(monitor, store):
    if monitor is None:
        return []
    result = []
    for session in monitor.status().get("sessions", []):
        if session.get("service") == "codex-review":
            continue
        latest = session.get("last_activity") or {}
        updated = int(latest.get("time_unix_nano") or 0)
        state = session.get("run_state") or "unknown"
        if state in {"running", "waiting"} and updated and time.time_ns() - updated > 35_000_000_000:
            state = "no updates"
        result.append({"runtime": session.get("runtime"), "session_id": session.get("session_id"),
                       "project": session.get("project"), "task": session.get("task"),
                       "goal": session.get("goal"), "state": state,
                       "waiting_for": session.get("waiting_for") if state == "waiting" else "",
                       "updated_ns": updated, "attention": [], "can_reply": False,
                       "source_id": session.get("source_id")})
    return result


def native_detail(store, session_id):
    page = store.activity_page(limit=150, session_id=session_id, include_reviews=False)
    events = sorted(page["activity"], key=lambda event: event.get("time_unix_nano", 0))
    goal = next((event.get("detail") for event in events if event.get("domain") == "message"
                 and event.get("action") == "user"), "")
    return {"goal": goal, "events": events}


def summarize(session, events):
    evidence = summary_evidence({"runtime": session.get("runtime"), "project": session.get("project"),
        "task": session.get("goal") or session.get("task"), "model": session.get("model"),
        "state": session.get("state"), "waiting_for": session.get("waiting_for"),
        "stamp": session.get("updated_ns"), "session_id": session.get("session_id"),
        "service": session.get("service")}, [dict(event, session_id=session.get("session_id")) for event in events])
    return generate_summary(SummaryConfig.from_env(), evidence)
