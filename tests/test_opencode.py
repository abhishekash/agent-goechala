"""OpenCode's local database is observed read-only and normalized to traces."""
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from observatory.core import Store
from observatory.opencode import OpenCodeReader, _opencode_mcp_attributes


class OpenCodeReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / "opencode.db"
        self.store = Store(self.root / "observatory.db")
        self.addCleanup(self.store.close)
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

    def test_unrelated_database_writes_do_not_reingest_messages(self):
        reader = OpenCodeReader(self.store, path=self.database)
        reader.poll()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE unrelated(value TEXT)")
        with patch.object(self.store, 'ingest') as ingest, patch.object(self.store, 'ingest_logs') as logs:
            self.assertEqual(reader.poll(), 0)
            ingest.assert_not_called()
            logs.assert_not_called()
        self.assertEqual(len(reader._message_revisions), 1)
        self.assertTrue(all(isinstance(value, bytes) and len(value) == 32
                            for value in reader._message_revisions.values()))

    def test_unchanged_database_does_not_open_a_connection(self):
        reader = OpenCodeReader(self.store, path=self.database)
        reader.poll()
        with patch('observatory.opencode.sqlite3.connect') as connect:
            self.assertEqual(reader.poll(), 0)
        connect.assert_not_called()

    def test_message_revision_cache_stays_within_the_import_window(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            template = db.execute('SELECT * FROM message').fetchone()
            for index in range(4):
                db.execute('INSERT INTO message VALUES(?,?,?,?,?)',
                           (f'message-{index}', *template[1:]))
        reader = OpenCodeReader(self.store, path=self.database)
        with patch('observatory.opencode.MAX_MESSAGES_PER_SESSION', 2):
            reader.poll()
        self.assertEqual(len(reader._message_revisions), 2)

    def test_part_changes_without_session_or_message_updates_are_captured(self):
        for timestamped in (False, True):
            with self.subTest(timestamped=timestamped):
                if timestamped:
                    with closing(sqlite3.connect(self.database)) as db, db:
                        db.execute('ALTER TABLE part ADD COLUMN time_updated INTEGER DEFAULT 1')
                reader = OpenCodeReader(self.store, path=self.database)
                reader.poll()
                with closing(sqlite3.connect(self.database)) as db, db:
                    data = json.loads(db.execute('SELECT data FROM part').fetchone()[0])
                    data['state']['output'] = {'count': 4 if timestamped else 3}
                    db.execute('UPDATE part SET data=?', (json.dumps(data),))
                    if timestamped:
                        db.execute('UPDATE part SET time_updated=2')
                self.assertGreater(reader.poll(), 0)
                output = self.store.mcp_page()['calls'][0]['output']
                self.assertIn(str(data['state']['output']['count']), str(output))

    def test_timestamped_parts_do_not_reload_unchanged_payloads(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('ALTER TABLE part ADD COLUMN time_updated INTEGER DEFAULT 1')
        reader = OpenCodeReader(self.store, path=self.database)
        reader.poll()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('UPDATE session SET time_updated=time_updated+1')
        statements = []
        connect = sqlite3.connect
        def traced_connect(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch('observatory.opencode.sqlite3.connect', side_effect=traced_connect):
            self.assertEqual(reader.poll(), 0)
        part_reads = [sql for sql in statements if 'FROM "part"' in sql]
        self.assertTrue(part_reads)
        self.assertTrue(all('"data"' not in sql for sql in part_reads))

    def test_unpopulated_part_timestamps_fall_back_to_content_comparison(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('ALTER TABLE part ADD COLUMN time_updated INTEGER')
        reader = OpenCodeReader(self.store, path=self.database)
        reader.poll()
        with closing(sqlite3.connect(self.database)) as db, db:
            data = json.loads(db.execute('SELECT data FROM part').fetchone()[0])
            data['state']['output'] = 'changed without a timestamp'
            db.execute('UPDATE part SET data=?', (json.dumps(data),))
        self.assertGreater(reader.poll(), 0)
        self.assertIn('changed without a timestamp', str(self.store.mcp_page()['calls'][0]['output']))

    def test_mcp_configuration_changes_reprocess_existing_messages(self):
        reader = OpenCodeReader(self.store, path=self.database)
        reader.poll()
        reader.set_mcp_servers(['github'])
        self.assertGreater(reader.poll(), 0)

    def test_revision_cache_is_pruned_and_missing_database_resets_it(self):
        reader = OpenCodeReader(self.store, path=self.database)
        reader.poll()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('DELETE FROM session')
        reader.poll()
        self.assertEqual(reader._message_revisions, {})
        self.assertEqual(reader._progress_revisions, {})
        self.database.unlink()
        reader.poll()
        self.assertIsNone(reader.fingerprint)

    def test_failed_ingestion_does_not_cache_uncommitted_revisions(self):
        reader = OpenCodeReader(self.store, path=self.database)
        with patch.object(self.store, 'ingest', side_effect=RuntimeError('busy')):
            with self.assertRaises(RuntimeError):
                reader.poll()
        self.assertEqual(reader._message_revisions, {})
        self.assertGreater(reader.poll(), 0)

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
