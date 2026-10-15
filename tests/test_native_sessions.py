"""Native session records must preserve MCP identity and call lifecycle."""
import json
import tempfile
import unittest
from pathlib import Path

from observatory.core import Store
from observatory.local_agents import Session


class NativeSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "events.db")

    def session(self, runtime, details=True):
        path = self.root / (runtime + ".jsonl")
        path.touch()
        return Session(self.store, path, runtime, capture_details=details)

    def append(self, session, *rows):
        with session.path.open("a") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        session.poll()
        self.assertEqual(session.skipped, 0)

    def claude(self, kind, content, second, **extra):
        return {"type": kind, "timestamp": f"2026-10-15T12:00:{second:02d}Z",
                "uuid": str(second), "sessionId": "claude-session", "cwd": "/work/project",
                "message": {"content": content}, **extra}

    def codex(self, payload, second, kind="response_item"):
        return {"type": kind, "timestamp": f"2026-10-15T12:00:{second:02d}Z", "payload": payload}

    def test_claude_mcp_start_result_stays_one_call_with_full_identity(self):
        session = self.session("Claude Code")
        name = "mcp__plugin_project-github__search_issues"
        self.append(session, self.claude("user", "Inspect issues", 0),
            self.claude("assistant", [{"type": "tool_use", "id": "call-1", "name": name,
                                      "input": {"query": "bug"}}], 1))
        call, = self.store.mcp_calls()
        self.assertEqual((call["server"], call["tool"], call["status"]),
                         ("plugin_project-github", "search_issues", "RUNNING"))
        call_id = call["id"]
        self.append(session, self.claude("user", [{"type": "tool_result", "tool_use_id": "call-1",
                      "content": "Permission denied", "is_error": True}], 3))
        call, = self.store.mcp_calls()
        self.assertEqual(call["id"], call_id)
        self.assertEqual(call["status"], "ERROR")
        self.assertEqual(call["duration_ms"], 2000)
        self.assertIn("bug", str(call["arguments"]))
        self.assertIn("Permission denied", str(call["output"]))
        self.assertEqual(call["source"], "session log + span")
        # Re-reading a file after restart must not create duplicate calls.
        replay = Session(self.store, session.path, "Claude Code")
        replay.poll()
        self.assertEqual(len(self.store.mcp_calls()), 1)

    def test_codex_function_mcp_and_normal_tool_classification(self):
        session = self.session("Codex")
        self.append(session,
            self.codex({"turn_id": "turn-1"}, 0, "turn_context"),
            self.codex({"type": "function_call", "call_id": "call-1", "name": "mcp__my_server__read_file",
                        "arguments": '{"path":"/docs/README.md"}'}, 1),
            self.codex({"type": "function_call", "call_id": "shell-1", "name": "exec_command",
                        "arguments": '{"cmd":"pwd"}'}, 2))
        call, = self.store.mcp_calls()
        self.assertEqual((call["server"], call["tool"]), ("my_server", "read_file"))
        self.append(session, self.codex({"type": "function_call_output", "call_id": "call-1",
                        "output": '{"isError":true,"content":[]}'}, 4))
        call, = self.store.mcp_calls()
        self.assertEqual(call["status"], "ERROR")
        self.assertEqual(call["duration_ms"], 3000)
        self.assertIn("README.md", str(call["arguments"]))

    def test_parallel_claude_calls_do_not_collide_and_metadata_stays_private(self):
        session = self.session("Claude Code", details=False)
        self.append(session, self.claude("assistant", [
            {"type": "tool_use", "id": key, "name": "mcp__github__search", "input": {"query": "private-query"}}
            for key in ("one", "two")], 1))
        self.assertEqual(len(self.store.mcp_calls()), 2)
        self.append(session, self.claude("user", [
            {"type": "tool_result", "tool_use_id": key, "content": "private-result"}
            for key in ("one", "two")], 2))
        calls = self.store.mcp_calls()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["status"] == "OK" for call in calls))
        self.assertNotIn("private-", json.dumps(calls))

    def test_codex_explicit_mcp_is_error_updates_span(self):
        session = self.session("Codex")
        self.append(session, self.codex({"type": "item_completed", "item": {
            "type": "McpToolCall", "id": "one", "server": "github", "tool": "search",
            "status": "completed", "result": {"isError": True}}}, 1, "event_msg"))
        call, = self.store.mcp_calls()
        self.assertEqual(call["status"], "ERROR")
        span, = self.store.trace(call["trace_id"])["spans"]
        self.assertEqual(span["status"], "ERROR")

    def test_session_status_waiting_completion_and_project_preservation(self):
        session = self.session('Codex')
        self.append(session, self.codex({'id': 'session', 'cwd': '/work/repo',
                    'git': {'branch': 'feature/mcp'}}, 0, 'session_meta'),
                    self.codex({'turn_id': 'turn-1'}, 1, 'turn_context'),
                    self.codex({'type': 'task_started'}, 2, 'event_msg'))
        self.assertEqual((session.project, session.branch, session.run_state), ('repo', 'feature/mcp', 'running'))
        self.append(session, self.codex({'type': 'function_call', 'call_id': 'input',
                    'name': 'functions.request_user_input', 'arguments': '{}'}, 3))
        self.assertEqual((session.run_state, session.waiting_for), ('waiting', 'User input'))
        self.append(session, self.codex({'type': 'function_call_output', 'call_id': 'input', 'output': 'yes'}, 4))
        self.assertEqual((session.run_state, session.waiting_for), ('running', ''))
        self.append(session, self.codex({'type': 'task_complete'}, 5, 'event_msg'))
        self.assertEqual((session.run_state, session.waiting_for), ('completed', ''))
        self.assertEqual(session.working_directory, '/work/repo')

    def test_mcp_inventory_covers_history_and_respects_scope(self):
        from observatory.core import normalize_native
        spans = []
        for i in range(105):
            spans.append(normalize_native({'trace_id': f'{i+1:032x}', 'span_id': f'{i+1:016x}',
                'name': 'tool', 'start_unix_nano': (i+1)*1_000_000_000, 'end_unix_nano': (i+1)*1_000_000_000,
                'status': 'ERROR' if i == 0 else 'OK',
                'attributes': {'mcp.server.name': 'older-server' if i == 0 else 'github',
                               'mcp.tool.name': 'search', 'session.id': 'a' if i < 104 else 'b'}}, 'codex'))
        self.store.ingest(spans)
        page = self.store.mcp_page(limit=100)
        self.assertEqual(len(page['calls']), 100)
        self.assertEqual(page['summary']['calls'], 105)
        self.assertEqual(page['summary']['errors'], 1)
        self.assertEqual({row['server'] for row in page['summary']['servers']}, {'github', 'older-server'})
        self.assertEqual(self.store.mcp_page(session_id='b')['summary']['calls'], 1)
        # Search and pagination change the table, not the scope-wide inventory.
        self.assertEqual(self.store.mcp_page(query='no-matches')['summary'], page['summary'])
        self.assertEqual(self.store.mcp_page(cursor=page['next_cursor'])['summary'], page['summary'])
