import json
import io
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from observatory.control import ControlBroker, ControlError, OpenCode, claude_hook_response, native_detail
from observatory.controlled import CodexBridge, claude_settings
from observatory.core import Store
from observatory.local_agents import Session
from observatory.server import handler_for


class OpenCodeTests(unittest.TestCase):
    def test_rejects_nonlocal_or_credentialed_endpoint(self):
        for url in ("https://example.com", "http://127.0.0.1:4096/path", "http://user:pass@localhost:4096"):
            with self.subTest(url=url), self.assertRaises(ControlError):
                OpenCode(url)

    def test_pending_request_must_still_belong_to_session(self):
        client = OpenCode("http://127.0.0.1:4096")
        calls = []

        def request(method, path, body=None):
            calls.append((method, path, body))
            if path == "/permission":
                return [{"id": "p1", "sessionID": "one"}]
            return True

        client.request = request
        with self.assertRaisesRegex(ControlError, "no longer pending"):
            client.reply("two", "permission", "p1", decision="once")
        self.assertEqual(len(calls), 1)
        self.assertEqual(client.reply("one", "permission", "p1", decision="once"), {"accepted": True})
        self.assertEqual(calls[-1], ("POST", "/session/one/permissions/p1", {"response": "once"}))

    def test_permission_reply_falls_back_to_legacy_open_code_api(self):
        client = OpenCode("http://127.0.0.1:4096")
        calls = []

        def request(method, path, body=None):
            calls.append((method, path, body))
            if path == "/permission":
                return [{"id": "p1", "sessionID": "one"}]
            if path == "/session/one/permissions/p1":
                raise ControlError("OpenCode returned HTTP 404")
            return True

        client.request = request
        client.reply("one", "permission", "p1", decision="once")
        self.assertEqual(calls[-1], ("POST", "/permission/p1/reply", {"reply": "once"}))

    def test_question_requires_each_answer(self):
        client = OpenCode("http://127.0.0.1:4096")
        calls = []

        def request(method, path, body=None):
            calls.append((method, path, body))
            if path == "/question":
                return [{"id": "q1", "sessionID": "one", "questions": [{"question": "A?"}]}]
            return True

        client.request = request
        with self.assertRaisesRegex(ControlError, "Answer each"):
            client.reply("one", "question", "q1", answers=[])
        client.reply("one", "question", "q1", answers=[["yes"]])
        self.assertEqual(calls[-1], ("POST", "/session/one/question/q1/reply", {"answers": [["yes"]]}))

    def test_goal_and_error_from_open_code_messages(self):
        client = OpenCode("http://127.0.0.1:4096")
        client.request = lambda method, path, body=None: [
            {"info": {"role": "user", "time": {"created": 1000}}, "parts": [{"type": "text", "text": "Build a dashboard"}]},
            {"info": {"role": "assistant", "time": {"created": 2000}, "error": {"message": "failed"}},
             "parts": [{"type": "tool", "tool": "bash", "state": {"status": "error", "error": "exit 1"}}]},
        ]
        detail = client.detail("session")
        self.assertEqual(detail["goal"], "Build a dashboard")
        self.assertEqual(sum(event["result"] == "error" for event in detail["events"]), 2)


class NativeGoalTests(unittest.TestCase):
    def test_first_prompt_remains_goal_after_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.touch()
            store = Store(Path(tmp) / "events.db")
            session = Session(store, path, "Codex")
            rows = [
                {"timestamp": "2026-10-17T12:00:00Z", "type": "event_msg", "payload": {"type": "task_started"}},
                {"timestamp": "2026-10-17T12:00:01Z", "type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "Build a dashboard"}]}}},
                {"timestamp": "2026-10-17T12:00:02Z", "type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "Use blue"}]}}},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            session.poll()
            self.assertEqual(session.goal, "Build a dashboard")
            self.assertEqual(session.tasks[session.turn], "Use blue")
            self.assertEqual(native_detail(store, session.session_id)["goal"], "Build a dashboard")


class BrokerTests(unittest.TestCase):
    def test_reply_is_bound_to_runtime_session_and_request(self):
        broker = ControlBroker()
        request = broker.register("Claude Code", "session-one", "permission", {"permission": "Bash"})
        with self.assertRaisesRegex(ControlError, "does not belong"):
            broker.reply("Claude Code", "session-two", "permission", request["id"], decision="once")
        self.assertEqual(len(broker.list()), 1)
        broker.reply("Claude Code", "session-one", "permission", request["id"], decision="once")
        with self.assertRaisesRegex(ControlError, "no longer pending"):
            broker.poll(request["id"], "wrong", timeout=0)
        self.assertEqual(broker.poll(request["id"], request["owner_token"], timeout=0),
                         {"pending": False, "decision": "once"})
        self.assertEqual(broker.list(), [])
        with self.assertRaisesRegex(ControlError, "no longer pending"):
            broker.reply("Claude Code", "session-one", "permission", request["id"], decision="once")

    def test_claude_hook_answers_question_and_permission(self):
        permission = {"hook_event_name": "PermissionRequest", "tool_name": "Bash"}
        self.assertEqual(claude_hook_response(permission, {"decision": "once"})["hookSpecificOutput"]["decision"]["behavior"], "allow")
        self.assertEqual(claude_hook_response(permission, None)["hookSpecificOutput"]["decision"]["behavior"], "deny")
        question = {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "tool_input": {
            "questions": [{"question": "Framework?", "options": [{"label": "React"}]}]}}
        output = claude_hook_response(question, {"answers": [["React"]]})["hookSpecificOutput"]
        self.assertEqual(output["updatedInput"]["answers"], {"Framework?": "React"})
        settings = claude_settings()
        self.assertIn("PermissionRequest", settings["hooks"])
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["matcher"], "AskUserQuestion")

    def test_codex_response_matches_generated_schema_shape(self):
        method = "item/commandExecution/requestApproval"
        self.assertEqual(CodexBridge.answer(method, {"decision": "accept"}), {"decision": "accept"})
        self.assertEqual(CodexBridge.answer(method, None), {"decision": "decline"})
        questions = [{"id": "color", "question": "Which color?"}]
        self.assertEqual(CodexBridge.answer("item/tool/requestUserInput", {"answers": [["blue"]]}, questions),
                         {"answers": {"color": {"answers": ["blue"]}}})


class ControlHTTPTests(unittest.TestCase):
    def test_authenticated_control_routes_and_one_time_reply(self):
        class Socket:
            def __init__(self, raw):
                self.input, self.output = io.BytesIO(raw), io.BytesIO()
            def makefile(self, mode, *args, **kwargs):
                return self.input if "r" in mode else self.output
            def sendall(self, data):
                self.output.write(data)
        class Server:
            server_name, server_port = "localhost", 4318
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"OBSERVATORY_OPENCODE_URL": ""}):
            handler = handler_for(Store(Path(tmp) / "events.db"), "secret", quiet=True)
            def request(path, body=None, token="secret", origin=None):
                data = json.dumps(body).encode() if body is not None else None
                method = "POST" if data is not None else "GET"
                headers = "Host: localhost:4318\r\n"
                if token:
                    headers += "Authorization: Bearer " + token + "\r\n"
                if origin:
                    headers += "Origin: " + origin + "\r\n"
                if data is not None:
                    headers += "Content-Type: application/json\r\nContent-Length: " + str(len(data)) + "\r\n"
                sock = Socket(f"{method} {path} HTTP/1.1\r\n{headers}\r\n".encode() + (data or b""))
                handler(sock, ("127.0.0.1", 1234), Server())
                head, payload = sock.output.getvalue().split(b"\r\n\r\n", 1)
                return int(head.split()[1]), json.loads(payload) if payload else None
            self.assertEqual(request("/api/control", token=None)[0], 401)
            self.assertEqual(request("/api/control")[1]["sessions"], [])
            self.assertEqual(request("/api/control/request", {"runtime": "Codex", "session_id": "thread-1",
                "kind": "permission", "detail": {"permission": "shell"}}, origin="https://other.example")[0], 403)
            code, item = request("/api/control/request", {"runtime": "Codex", "session_id": "thread-1",
                "kind": "permission", "detail": {"permission": "shell"}})
            self.assertEqual(code, 200)
            self.assertNotIn("owner_token", str(request("/api/control")[1]))
            self.assertEqual(request("/api/control/reply", {"runtime": "Codex", "session_id": "other",
                "kind": "permission", "request_id": item["id"], "decision": "accept"})[0], 400)
            self.assertEqual(request("/api/control/reply", {"runtime": "Codex", "session_id": "thread-1",
                "kind": "permission", "request_id": item["id"], "decision": "accept"})[0], 200)
            self.assertEqual(request("/api/control/decision?id=" + item["id"] + "&owner=wrong")[0], 404)
            self.assertEqual(request("/api/control/decision?id=" + item["id"] + "&owner=" + item["owner_token"])[1],
                             {"pending": False, "decision": "accept"})
            self.assertEqual(request("/api/control/reply", {"runtime": "Codex", "session_id": "thread-1",
                "kind": "permission", "request_id": item["id"], "decision": "accept"})[0], 400)
            hook_result = {}
            payload = {"session_id": "claude-1", "hook_event_name": "PermissionRequest",
                       "tool_name": "Bash", "tool_input": {"command": "npm test"}}
            hook = threading.Thread(target=lambda: hook_result.setdefault("response", request(
                "/api/control/claude-hook", payload)))
            hook.start()
            for _ in range(50):
                pending = request("/api/control")[1]["attention"]
                if pending:
                    break
                time.sleep(.01)
            else:
                self.fail("Claude hook did not register a pending request")
            self.assertEqual(pending[0]["runtime"], "Claude Code")
            self.assertEqual(request("/api/control/reply", {"runtime": "Claude Code", "session_id": "claude-1",
                "kind": "permission", "request_id": pending[0]["id"], "decision": "once"})[0], 200)
            hook.join(timeout=2)
            self.assertFalse(hook.is_alive())
            self.assertEqual(hook_result["response"][1]["hookSpecificOutput"]["decision"]["behavior"], "allow")

