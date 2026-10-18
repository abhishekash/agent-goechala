"""Launch Codex and Claude Code with an Observatory-owned reply channel."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .control import ControlError, OpenCode, claude_hook_response


def endpoint(value=None):
    value = (value or os.environ.get("OBSERVATORY_ENDPOINT") or "http://127.0.0.1:4318").rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or (
            parsed.path not in {"", "/"} or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ControlError("controlled agents require a local Observatory receiver URL")
    return value


def api(path, body=None, *, base=None, timeout=30):
    base = endpoint(base)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    token = os.environ.get("OBSERVATORY_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(base + path, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ControlError("Observatory response exceeded 2 MB")
        return json.loads(raw) if raw else None
    except HTTPError as exc:
        raise ControlError(f"Observatory returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise ControlError("Observatory receiver is unavailable") from None


def claude_settings():
    hook = {"type": "command", "command": sys.executable,
            "args": ["-m", "observatory", "claude-hook"], "timeout": 610}
    return {"hooks": {"PermissionRequest": [{"hooks": [hook]}],
                      "PreToolUse": [{"matcher": "AskUserQuestion", "hooks": [hook]}]}}


def launch_claude(arguments, *, base=None, cwd=None):
    base = endpoint(base)
    api("/api/health", base=base)
    with tempfile.TemporaryDirectory(prefix="observatory-claude-") as root:
        settings = Path(root) / "settings.json"
        settings.write_text(json.dumps(claude_settings()), encoding="utf-8")
        settings.chmod(0o600)
        env = {**os.environ, "OBSERVATORY_ENDPOINT": base}
        command = ["claude", "--settings", str(settings), *arguments]
        return subprocess.call(command, env=env, cwd=cwd)


def run_claude_hook():
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ControlError("invalid Claude hook payload")
    try:
        result = api("/api/control/claude-hook", payload, timeout=620)
    except ControlError:
        result = claude_hook_response(payload, None)
    print(json.dumps(result), flush=True)


class CodexBridge:
    def __init__(self, prompt, *, base=None, cwd=None, quiet=False):
        self.prompt = prompt
        self.base = endpoint(base)
        self.cwd = str(Path(cwd or os.getcwd()).resolve())
        self.quiet = quiet
        self.process = None
        self.write_lock = threading.Lock()
        self.pending = set()
        self.events = []

    def send(self, message):
        with self.write_lock:
            if not self.process or not self.process.stdin:
                raise ControlError("Codex app-server is not running")
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()

    def read(self):
        if not self.process or not self.process.stdout:
            raise ControlError("Codex app-server is not running")
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise ControlError("Codex app-server closed its control stream")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                return message

    def rpc(self, ident, method, params):
        self.send({"id": ident, "method": method, "params": params})
        while True:
            message = self.read()
            if "method" in message and "id" in message:
                self.handle_request(message)
            elif message.get("id") == ident:
                if message.get("error"):
                    raise ControlError("Codex rejected " + method + ": " + str(message["error"]))
                return message.get("result") or {}
            else:
                self.events.append(message)

    @staticmethod
    def request_kind(method):
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            return "permission"
        if method == "item/tool/requestUserInput":
            return "question"
        return None

    @staticmethod
    def answer(method, decision, questions=None):
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            return {"decision": (decision or {}).get("decision", "decline")}
        if method == "item/tool/requestUserInput":
            groups = (decision or {}).get("answers") or []
            return {"answers": {question["id"]: {"answers": group}
                                for question, group in zip(questions or [], groups)}}
        return None

    def handle_request(self, message):
        method, params = message.get("method"), message.get("params") or {}
        kind = self.request_kind(method)
        if not kind or not isinstance(params, dict):
            self.send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported request"}})
            return
        def work():
            request_id = None
            try:
                detail = ({"questions": params.get("questions") or []} if kind == "question" else
                          {"permission": params.get("reason") or method,
                           "metadata": {key: value for key, value in params.items()
                                        if key not in {"threadId", "turnId"}}})
                item = api("/api/control/request", {"runtime": "Codex", "session_id": params["threadId"],
                                                    "kind": kind, "detail": detail}, base=self.base)
                request_id = item["id"]
                owner_token = item["owner_token"]
                self.pending.add((request_id, owner_token))
                while True:
                    response = api("/api/control/decision?" + urlencode({"id": request_id, "owner": owner_token}),
                                   base=self.base, timeout=25)
                    if not response.get("pending"):
                        break
                result = self.answer(method, response, params.get("questions"))
            except (ControlError, KeyError, TypeError, ValueError):
                result = self.answer(method, None, params.get("questions"))
            finally:
                if request_id:
                    self.pending.discard((request_id, owner_token))
                    try:
                        api("/api/control/cancel", {"request_id": request_id, "owner_token": owner_token}, base=self.base)
                    except ControlError:
                        pass
            try:
                self.send({"id": message["id"], "result": result})
            except (ControlError, BrokenPipeError, OSError):
                pass
        threading.Thread(target=work, daemon=True).start()

    def run(self):
        api("/api/health", base=self.base)
        self.process = subprocess.Popen(["codex", "app-server", "--listen", "stdio://"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=sys.stderr, text=True, bufsize=1, cwd=self.cwd)
        try:
            self.rpc(1, "initialize", {"clientInfo": {"name": "agent_observatory", "title": "Agent Observatory",
                                                       "version": "0.1.2"},
                                        "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized", "params": {}})
            started = self.rpc(2, "thread/start", {"cwd": self.cwd})
            thread_id = started["thread"]["id"]
            self.rpc(3, "turn/start", {"threadId": thread_id,
                                       "input": [{"type": "text", "text": self.prompt}]})
            if not self.quiet:
                print("Codex session " + thread_id + " is visible in Observatory Control Center", flush=True)
            while True:
                message = self.events.pop(0) if self.events else self.read()
                method, params = message.get("method"), message.get("params") or {}
                if "id" in message and method:
                    self.handle_request(message)
                elif method == "item/agentMessage/delta":
                    if not self.quiet:
                        sys.stdout.write(str(params.get("delta") or ""))
                        sys.stdout.flush()
                elif method == "turn/completed" and params.get("threadId") == thread_id:
                    status = str((params.get("turn") or {}).get("status") or "unknown")
                    if not self.quiet:
                        print("\nTurn " + status, flush=True)
                    return 0 if status == "completed" else 1
        finally:
            for request_id, owner_token in list(self.pending):
                try:
                    api("/api/control/cancel", {"request_id": request_id, "owner_token": owner_token}, base=self.base)
                except ControlError:
                    pass
            if self.process:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()


class SessionLauncher:
    """Start controlled agent sessions from the browser Control Center."""

    def __init__(self, opencode, *, base=None, cwd=None):
        self.opencode = opencode
        self.base = endpoint(base)
        self.cwd = str(Path(cwd or os.getcwd()).resolve())
        self.lock = threading.Lock()
        self.jobs = {}

    def available(self):
        return {"OpenCode": self.opencode.enabled,
                "Codex": shutil.which("codex") is not None,
                "Claude Code": shutil.which("claude") is not None}

    def list(self):
        with self.lock:
            return sorted((dict(job) for job in self.jobs.values()),
                          key=lambda job: job["created_ns"], reverse=True)[:10]

    def start(self, runtime, prompt):
        if runtime not in {"OpenCode", "Codex", "Claude Code"}:
            raise ControlError("Choose OpenCode, Codex, or Claude Code")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
            raise ControlError("Enter a prompt of 1 to 12000 characters")
        prompt = prompt.strip()
        if runtime == "OpenCode":
            return self.opencode.start(prompt)
        executable = shutil.which("codex" if runtime == "Codex" else "claude")
        if not executable:
            raise ControlError(runtime + " CLI is not installed")
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "runtime": runtime, "state": "starting", "error": "",
               "created_ns": time.time_ns()}
        with self.lock:
            self.jobs[job_id] = job
        worker = threading.Thread(target=self._run, args=(job_id, runtime, prompt),
                                  name="observatory-launch-" + runtime.lower().replace(" ", "-"), daemon=True)
        worker.start()
        return {"runtime": runtime, "job_id": job_id, "state": "starting"}

    def _run(self, job_id, runtime, prompt):
        with self.lock:
            self.jobs[job_id]["state"] = "running"
        try:
            if runtime == "Codex":
                status = CodexBridge(prompt, base=self.base, cwd=self.cwd, quiet=True).run()
            else:
                status = launch_claude(["--print", prompt], base=self.base, cwd=self.cwd)
            with self.lock:
                job = self.jobs[job_id]
                job["state"] = "completed" if status == 0 else "failed"
                if status:
                    job["error"] = runtime + " exited with status " + str(status)
        except Exception as exc:
            with self.lock:
                job = self.jobs[job_id]
                job["state"] = "failed"
                job["error"] = str(exc)[:500] or type(exc).__name__
