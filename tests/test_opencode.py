"""OpenCode's local database is observed read-only and normalized to traces."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from observatory.core import Store
from observatory.opencode import OpenCodeReader, _opencode_mcp_attributes


class OpenCodeReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / "opencode.db"
        self.store = Store(self.root / "observatory.db")
        db = sqlite3.connect(self.database)
        db.executescript("""
            CREATE TABLE project(id TEXT PRIMARY KEY, name TEXT, worktree TEXT);
            CREATE TABLE session(id TEXT PRIMARY KEY, project_id TEXT, title TEXT, directory TEXT,
                time_created INTEGER, time_updated INTEGER, agent TEXT, model TEXT,
                tokens_input INTEGER, tokens_output INTEGER);
            CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
                time_updated INTEGER, data TEXT);
            CREATE TABLE part(id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT,
                time_created INTEGER, data TEXT);
            CREATE TABLE todo(session_id TEXT, content TEXT, status TEXT, position INTEGER,
                time_updated INTEGER);
        """)
        now_ms = time.time_ns() // 1_000_000
        db.execute("INSERT INTO project VALUES(?,?,?)", ("p1", "sample-repo", "/work/sample-repo"))
        db.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)", (
            "ses_test", "p1", "Fix the MCP integration", "/work/sample-repo", now_ms-3000,
            now_ms, "build", json.dumps({"id": "model-x", "providerID": "provider-x"}), 12, 7))
        db.execute("INSERT INTO message VALUES(?,?,?,?,?)", (
            "msg_test", "ses_test", now_ms-2000, now_ms,
            json.dumps({"role": "assistant", "modelID": "model-x", "providerID": "provider-x",
                "finish": "stop", "cost": 0.0123, "time": {"created": now_ms-2000, "completed": now_ms},
                "tokens": {"input": 12, "output": 7, "reasoning": 2, "cache": {"read": 5}}})))
        db.execute("INSERT INTO part VALUES(?,?,?,?,?)", (
            "prt_test", "ses_test", "msg_test", now_ms-1000,
            json.dumps({"type": "tool", "tool": "mcp__github__search_issues",
                "state": {"status": "completed", "time": {"start": now_ms-900, "end": now_ms-800},
                          "input": {"query": "bug", "Authorization": "secret"}, "output": {"count": 2}}})))
        db.executemany("INSERT INTO todo VALUES(?,?,?,?,?)", [
            ("ses_test", "Inspect the MCP call", "completed", 0, now_ms-1000),
            ("ses_test", "Fix the server mapping", "in_progress", 1, now_ms-500),
            ("ses_test", "Run the regression tests", "pending", 2, now_ms-250),
        ])
        db.commit()
        db.close()

    def test_read_only_capture_builds_session_model_and_mcp_spans(self):
        reader = OpenCodeReader(self.store, path=self.database)
        self.assertGreater(reader.poll(), 0)
        self.assertEqual(reader.poll(), 0)
        self.assertEqual(len(reader.sessions), 1)
        self.assertEqual(reader.discovered_sessions, 1)
        self.assertEqual(reader.omitted_sessions, 0)
        self.assertEqual(reader.sessions[0]["runtime"], "OpenCode")
        self.assertEqual(reader.sessions[0]["project"], "sample-repo")
        self.assertEqual(reader.sessions[0]["task"], "Fix the MCP integration")
        self.assertEqual(reader.status_sessions()[0]["run_state"], "running")
        reader.sessions[0]["last_update_ns"] -= 40_000_000_000
        self.assertEqual(reader.status_sessions()[0]["run_state"], "idle")
        sessions = self.store.sessions()
        self.assertEqual(sessions[0]["session_id"], "ses_test")
        self.assertEqual((sessions[0]["input_tokens"], sessions[0]["output_tokens"]), (12, 7))
        self.assertAlmostEqual(sessions[0]["known_cost_usd"], 0.0123)
        db = sqlite3.connect(self.store.path)
        trace_id = db.execute("SELECT trace_id FROM spans WHERE name='agent.run'").fetchone()[0]
        root_id = db.execute("SELECT span_id FROM spans WHERE name='agent.run'").fetchone()[0]
        model_span = db.execute("SELECT span_id,parent_span_id FROM spans WHERE name='chat model-x'").fetchone()
        tool_span = db.execute("SELECT span_id,parent_span_id FROM spans WHERE name='mcp__github__search_issues'").fetchone()
        model_attributes = [json.loads(row[0]) for row in db.execute(
            "SELECT attributes FROM spans WHERE name='chat model-x'")]
        db.close()
        self.assertEqual(model_span[1], root_id)
        self.assertEqual(tool_span[1], model_span[0])
        progress = self.store.trace(trace_id)["progress"]
        self.assertEqual(len(progress), 1)
        self.assertAlmostEqual(progress[0]["percent"], 33.33)
        self.assertIn("Fix the server mapping", progress[0]["message"])
        self.assertIn("Run the regression tests", progress[0]["message"])
        self.assertEqual(model_attributes[0]["gen_ai.usage.cache_read.input_tokens"], 5)
        self.assertEqual(model_attributes[0]["gen_ai.usage.reasoning_tokens"], 2)
        mcp = self.store.mcp_page()
        self.assertEqual(mcp["summary"]["calls"], 1)
        self.assertEqual(mcp["calls"][0]["server"], "github")
        self.assertEqual(mcp["calls"][0]["tool"], "search_issues")
        self.assertIn("[REDACTED]", str(mcp["calls"][0]["arguments"]))
        db = sqlite3.connect(self.database)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM part").fetchone()[0], 1)
        db.close()

    def test_status_reports_sessions_omitted_by_recent_session_limit(self):
        db = sqlite3.connect(self.database)
        now_ms = time.time_ns() // 1_000_000
        db.executemany("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)", [
            (f'ses_extra_{index:02}', 'p1', 'Older task', '/work/sample-repo',
             now_ms-index*1000-10, now_ms-index*1000-1, 'build', 'model-x', 0, 0)
            for index in range(51)])
        db.commit()
        db.close()
        reader = OpenCodeReader(self.store, path=self.database)
        reader.poll()
        self.assertEqual(reader.discovered_sessions, 52)
        self.assertEqual(reader.omitted_sessions, 2)
        self.assertEqual(len(reader.status_sessions()), 50)

    def test_metadata_only_keeps_tool_identity_but_omits_payload_and_task(self):
        reader = OpenCodeReader(self.store, capture_details=False, path=self.database)
        reader.poll()
        self.assertEqual(reader.sessions[0]["task"], "")
        self.assertEqual(reader.sessions[0]["working_directory"], "")
        calls = self.store.mcp_page()["calls"]
        self.assertEqual(calls[0]["tool"], "search_issues")
        self.assertEqual(calls[0]["arguments"], "")
        self.assertEqual(calls[0]["output"], "")
        db = sqlite3.connect(self.store.path)
        trace_id = db.execute("SELECT trace_id FROM spans WHERE name='agent.run'").fetchone()[0]
        db.close()
        progress = self.store.trace(trace_id)["progress"]
        self.assertEqual(len(progress), 1)
        self.assertAlmostEqual(progress[0]["percent"], 33.33)
        self.assertNotIn("Fix the server mapping", progress[0]["message"])

    def test_todo_changes_append_grounded_progress_to_the_trace(self):
        reader = OpenCodeReader(self.store, path=self.database)
        reader.poll()
        db = sqlite3.connect(self.store.path)
        trace_id = db.execute("SELECT trace_id FROM spans WHERE name='agent.run'").fetchone()[0]
        db.close()
        db = sqlite3.connect(self.database)
        db.execute("UPDATE todo SET status='completed', time_updated=? WHERE session_id=? AND position=1",
                   (time.time_ns() // 1_000_000, "ses_test"))
        db.commit()
        db.close()
        self.assertGreater(reader.poll(), 0)
        progress = self.store.trace(trace_id)["progress"]
        self.assertEqual([event["percent"] for event in progress], [33.33, 66.67])

    def test_configured_server_prefix_identifies_opencode_mcp_tool(self):
        db = sqlite3.connect(self.database)
        now_ms = time.time_ns() // 1_000_000
        db.execute("INSERT INTO part VALUES(?,?,?,?,?)", (
            "prt_opencode_mcp", "ses_test", "msg_test", now_ms,
            json.dumps({"type": "tool", "tool": "mcp_everything_add",
                "state": {"status": "completed", "input": {"a": 3, "b": 4}, "output": 7}})))
        db.commit()
        db.close()

        reader = OpenCodeReader(self.store, path=self.database)
        reader.set_mcp_servers(["mcp_everything"])
        reader.poll()
        calls = self.store.mcp_page()["calls"]
        resolved = next(call for call in calls if call["tool"] == "add")
        self.assertEqual(resolved["server"], "mcp_everything")
        self.assertEqual(json.loads(resolved["arguments"]), {"a": "3", "b": "4"})

    def test_mcp_attribution_requires_a_unique_configured_prefix(self):
        self.assertEqual(_opencode_mcp_attributes("mcp_everything_add", ["mcp_everything"]), {
            "mcp.server.name": "mcp_everything", "mcp.tool.name": "add",
            "mcp.method.name": "tools/call"})
        self.assertEqual(_opencode_mcp_attributes("git_status", []), {})
        self.assertEqual(_opencode_mcp_attributes("same_tool", ["same", "same"]), {
            "mcp.server.name": "same", "mcp.tool.name": "tool",
            "mcp.method.name": "tools/call"})
        self.assertEqual(_opencode_mcp_attributes("foo_bar_baz", ["foo", "foo_bar"]), {
            "mcp.server.name": "foo_bar", "mcp.tool.name": "baz",
            "mcp.method.name": "tools/call"})
        self.assertEqual(_opencode_mcp_attributes("my_server_search", ["my-server"]), {
            "mcp.server.name": "my-server", "mcp.tool.name": "search",
            "mcp.method.name": "tools/call"})


if __name__ == "__main__":
    unittest.main()
