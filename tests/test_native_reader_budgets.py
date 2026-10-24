"""Resource/lifecycle regressions using only temporary synthetic producer data."""
import contextlib
import io
import json
import sqlite3
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from observatory.core import Store
from observatory.local_agents import Session, identity, timestamp
from observatory.local_state import RecentMap
from observatory.opencode import LOOKBACK_NS, OpenCodeReader
from observatory.watch import JsonlWatcher, start_watchers


class NativeBudgets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "observatory.db")
        self.addCleanup(self.store.close)

    def session(self, runtime):
        path = self.root / "session.jsonl"
        path.touch()
        return Session(self.store, path, runtime)

    def append(self, reader, rows):
        with reader.path.open("a") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        reader.poll()

    def codex(self, kind, **payload):
        return {"type": kind, "timestamp": "2026-10-24T12:00:00Z", "payload": payload}

    def assistant(self, key, output=10):
        return {"type": "assistant", "timestamp": "2026-10-24T12:00:00Z",
                "message": {"id": key, "usage": {"input_tokens": 20, "output_tokens": output}}}

    def test_unfinished_state_is_bounded_and_recent_late_result_survives_completion(self):
        with patch("observatory.local_agents.MAX_RECENT_TURNS", 3), patch("observatory.local_agents.MAX_RECENT_CALLS", 4):
            reader = self.session("Codex")
        rows = []
        for index in range(12):
            rows.extend([
                self.codex("turn_context", turn_id=str(index)),
                self.codex("event_msg", type="task_started"),
                self.codex("event_msg", type="item_completed", item={"type": "UserMessage", "content": "private prompt"}),
                self.codex("response_item", type="function_call", call_id=str(index), name="mcp__docs__read", arguments="{}")])
        self.append(reader, rows)
        self.assertLessEqual(len(reader.started), 3)
        self.assertLessEqual(len(reader.tasks), 3)
        for mapping in (reader.calls, reader.tool_inputs, reader.pending_tools):
            self.assertLessEqual(len(mapping), 4)
        self.append(reader, [self.codex("event_msg", type="task_complete"),
                             self.codex("turn_context", turn_id="next"),
                             self.codex("event_msg", type="task_started"),
                             self.codex("response_item", type="function_call_output", call_id="11", output="done")])
        self.assertNotIn("11", reader.calls)
        self.assertNotIn("11", reader.tool_inputs)
        trace = self.store.trace(identity(reader.source_id, "11"))
        tools = [span for span in trace["spans"] if span["name"] == "mcp__docs__read"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["status"], "OK")
        self.assertNotIn("private prompt", json.dumps(reader.diagnostics()))

    def test_eviction_does_not_reduce_persisted_or_same_poll_usage(self):
        with patch("observatory.local_agents.MAX_RECENT_USAGE", 2):
            reader = self.session("Claude Code")
        self.append(reader, [self.assistant("a"), self.assistant("b"), self.assistant("c"), self.assistant("a", 1)])
        self.assertEqual(len(reader.usage), 2)
        self.assertEqual(self.store.sessions()[0]["output_tokens"], 30)
        self.append(reader, [self.assistant("d"), self.assistant("e"), self.assistant("a", 2)])
        self.assertEqual(len(reader.usage), 2)
        self.assertEqual(self.store.sessions()[0]["output_tokens"], 50)
        self.assertGreater(reader.diagnostics()["cache_evictions"], 0)

    def test_claude_completed_turn_keeps_recent_tool_correlation(self):
        reader = self.session("Claude Code")
        stamp = "2026-10-24T12:00:00Z"
        self.append(reader, [
            {"type": "user", "uuid": "first", "timestamp": stamp, "message": {"content": "first task"}},
            {"type": "assistant", "timestamp": stamp, "message": {"stop_reason": "end_turn", "content": [
                {"type": "tool_use", "id": "late", "name": "mcp__docs__read", "input": {}}]}},
            {"type": "user", "uuid": "second", "timestamp": stamp, "message": {"content": "next task"}}])
        self.append(reader, [{"type": "user", "timestamp": stamp, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "late", "content": "done"}]}}])
        self.assertEqual(reader.calls, {})
        self.assertEqual(reader.tool_inputs, {})
        trace = self.store.trace(identity(reader.source_id, "first"))
        self.assertTrue(any(span["name"] == "mcp__docs__read" for span in trace["spans"]))

    def database(self, project_columns="id TEXT, name TEXT, worktree TEXT", project_values=("p", "project", "/work/tree")):
        path = self.root / "producer.db"
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.executescript(f'''CREATE TABLE project({project_columns});
                CREATE TABLE session(id TEXT, project_id TEXT, directory TEXT, time_updated INTEGER);
                CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
                CREATE TABLE part(id TEXT, session_id TEXT, message_id TEXT, data TEXT, time_updated INTEGER);''')
            db.execute('INSERT INTO project VALUES(' + ','.join('?' for _ in project_values) + ')', project_values)
            db.execute('INSERT INTO session VALUES(?,?,?,?)', ("s", "p", "/work/fallback", time.time_ns() // 1_000_000))
            db.execute('INSERT INTO message VALUES(?,?,?,?)', ("m", "s", time.time_ns() // 1_000_000,
                json.dumps({"role": "assistant", "tokens": {"input": 10}})))
        return path

    def test_optional_project_columns_and_empty_names(self):
        for columns, values, expected in [
            ("id TEXT", ("p",), "fallback"),
            ("id TEXT, name TEXT", ("p", "name-only"), "name-only"),
            ("id TEXT, worktree TEXT", ("p", "/work/tree"), "tree"),
            ("id TEXT, name TEXT", ("p", None), "fallback")]:
            with self.subTest(columns=columns, values=values):
                path = self.database(columns, values)
                reader = OpenCodeReader(self.store, path=path)
                self.assertGreater(reader.poll(), 0)
                self.assertEqual(reader.sessions[0]["project"], expected)
                self.assertEqual(reader.errors, 0)
                self.assertEqual(reader.schema_errors, 0)
                path.unlink()

    def test_unchanged_database_ages_out_and_prunes_revisions(self):
        path = self.database()
        reader = OpenCodeReader(self.store, path=path)
        now = time.time_ns()
        with patch("observatory.opencode.time.monotonic", return_value=100):
            self.assertGreater(reader.poll(), 0)
        fingerprint = reader.fingerprint
        with patch("observatory.opencode.time.monotonic", return_value=131), patch("observatory.opencode.time.time_ns", return_value=now + LOOKBACK_NS + 1_000_000_000):
            self.assertEqual(reader.poll(), 0)
            self.assertEqual(reader.status_sessions(), [])
        self.assertEqual(reader.fingerprint, fingerprint)
        self.assertEqual(reader.discovered_sessions, 0)
        self.assertEqual(reader.omitted_sessions, 0)
        self.assertEqual(reader._message_revisions, {})
        self.assertEqual(reader._progress_revisions, {})
        self.assertEqual(reader._part_revisions, {})
        self.assertEqual(reader._payload_cursors, {})

    def test_byte_preflight_excludes_large_unicode_before_decode_and_transfer(self):
        path = self.database()
        raw = json.dumps({"type": "tool", "state": {"output": "秘密" * 200}}, ensure_ascii=False)
        self.assertLess(len(raw), 1024)
        self.assertGreater(len(raw.encode()), 1024)
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute('INSERT INTO part VALUES(?,?,?,?,?)', ("p", "s", "m", raw, 1))
        reader = OpenCodeReader(self.store, path=path)
        statements = []
        connect = sqlite3.connect
        def traced(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch("observatory.opencode.MAX_PAYLOAD_BYTES", 1024), patch("observatory.opencode.sqlite3.connect", side_effect=traced), patch.object(reader, "_decode", wraps=reader._decode) as decode:
            reader.poll()
        self.assertTrue(all(call.args[0] != raw for call in decode.call_args_list))
        self.assertFalse(any(sql.startswith('SELECT "data" FROM "part"') for sql in statements))
        self.assertEqual(reader.skipped_oversized, 1)
        self.assertEqual(reader._part_revisions, {})
        self.assertNotIn("秘密", json.dumps(reader.diagnostics(), ensure_ascii=False))

    def test_aggregate_payload_budget_and_legacy_revision_fallback(self):
        path = self.database()
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            for index in range(20):
                raw = json.dumps({"type": "tool", "tool": "read", "state": {"output": "x" * 200}})
                db.execute('INSERT INTO part VALUES(?,?,?,?,?)', (str(index), "s", "m", raw, None))
        reader = OpenCodeReader(self.store, path=path)
        with patch("observatory.opencode.MAX_POLL_PAYLOAD_BYTES", 1000):
            reader.poll()
        self.assertLessEqual(reader.payload_bytes, 1000)
        self.assertGreater(reader.skipped_budget, 0)
        self.assertEqual(reader.errors, 0)

    def test_small_budget_eventually_imports_later_parts_messages_and_sessions(self):
        for revision_stamp in (None, 1):
            with self.subTest(revision_stamp=revision_stamp):
                path = self.database()
                with contextlib.closing(sqlite3.connect(path)) as db, db:
                    db.execute('DELETE FROM message')
                    for sid in ('s', 'later'):
                        if sid == 'later':
                            db.execute('INSERT INTO session VALUES(?,?,?,?)', (sid, 'p', '/work/later', time.time_ns() // 1_000_000 - 1))
                        for mid in (sid + '-one', sid + '-two'):
                            db.execute('INSERT INTO message VALUES(?,?,?,?)', (mid, sid, 1, '{"role":"assistant"}'))
                            for number in range(4):
                                raw = json.dumps({'type': 'tool', 'tool': mid + str(number),
                                    'state': {'status': 'completed', 'output': 'x' * 400}})
                                db.execute('INSERT INTO part VALUES(?,?,?,?,?)', (mid + str(number), sid, mid, raw, revision_stamp))
                reader = OpenCodeReader(self.store, path=path)
                captured = []
                with patch('observatory.opencode.MAX_POLL_PAYLOAD_BYTES', 1000), patch.object(self.store, 'ingest', side_effect=lambda spans: captured.extend(spans)):
                    for _ in range(40):
                        reader.fingerprint = None  # Also exercise forced refreshes.
                        reader.poll()
                        self.assertLessEqual(reader.payload_bytes, 1000)
                        names = {span['name'] for span in captured if span['name'] != 'agent.run'}
                        if len(names) == 16:
                            break
                    self.assertEqual(len(names), 16)
                    with contextlib.closing(sqlite3.connect(path)) as db, db:
                        raw = json.dumps({'type': 'tool', 'tool': 'changed-behind-cursor',
                            'state': {'status': 'completed', 'output': 'y' * 400}})
                        db.execute('UPDATE part SET data=?,time_updated=? WHERE id=?',
                            (raw, 2 if revision_stamp is not None else None, 's-one0'))
                    for _ in range(40):
                        reader.fingerprint = None
                        reader.poll()
                        if any(span['name'] == 'changed-behind-cursor' for span in captured):
                            break
                    self.assertTrue(any(span['name'] == 'changed-behind-cursor' for span in captured))
                self.assertLessEqual(len(reader._part_revisions), 16)
                self.assertTrue(all(isinstance(value, bytes) and len(value) == 32 for value in reader._part_revisions.values()))
                self.assertLessEqual(len(reader._payload_cursors), 6)
                with contextlib.closing(sqlite3.connect(path)) as db, db:
                    db.execute('DELETE FROM message')
                reader.fingerprint = None
                reader.poll()
                self.assertEqual(reader._part_revisions, {})
                self.assertEqual(reader._message_revisions, {})
                path.unlink()

    def test_recent_maps_enforce_entry_and_total_byte_limits_without_collisions(self):
        cache = RecentMap(512, max_bytes=4096, max_entry_bytes=2048)
        for number in range(100):
            cache[str(number)] = ('tool', 'x' * 500, str(number))
            self.assertLessEqual(cache.tracked_bytes, 4096)
        self.assertGreater(cache.evictions, 0)
        huge = 'a' * (2 * 1024 * 1024)
        cache[huge + 'one'] = 'tool'
        cache[huge + 'two'] = 'other tool'
        cache['huge-value'] = ('tool', huge, 'turn')
        self.assertEqual(cache.dropped_oversized, 3)
        self.assertNotIn('huge-value', cache)
        self.assertLessEqual(cache.tracked_bytes, 4096)
        cache.popitem()
        cache.pop(next(iter(cache)))
        cache.clear()
        self.assertEqual(cache.tracked_bytes, 0)

    def test_session_drops_huge_cached_identifiers_and_tool_names(self):
        reader = self.session('Codex')
        huge = 'a' * (100 * 1024)
        self.append(reader, [self.codex('response_item', type='function_call', call_id=huge, name='mcp__docs__read'),
                             self.codex('response_item', type='function_call', call_id='ordinary', name=huge)])
        for cache in (reader.calls, reader.tool_inputs, reader.pending_tools):
            self.assertLessEqual(cache.tracked_bytes, cache.max_bytes)
            self.assertEqual(len(cache), 0)
        self.assertGreater(reader.diagnostics()['cache_dropped_oversized_entries'], 0)

    def test_deep_json_and_extreme_timestamps_do_not_stop_watching(self):
        path = self.root / 'untrusted-spans.jsonl'
        good = {'trace_id': 'a' * 32, 'span_id': 'b' * 16, 'name': 'survivor',
                'start_unix_nano': 1, 'end_unix_nano': 2}
        deep = b'{"nested":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}'
        shallow_decoder_deep_consumer = b'{"nested":' + b'[' * 200 + b'0' + b']' * 200 + b'}'
        invalid = [deep, shallow_decoder_deep_consumer, b'[]']
        for stamp in (float('inf'), float('nan'), 10**100):
            invalid.append(json.dumps({**good, 'start_unix_nano': stamp, 'end_unix_nano': stamp}).encode())
        path.write_bytes(b'\n'.join([*invalid, json.dumps(good).encode()]) + b'\n')
        event = threading.Event()
        class Sink:
            def ingest(self, spans):
                event.set()
                return len(spans)
        reader = JsonlWatcher(Sink(), path)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reader.poll(), 1)
            self.assertEqual(reader.schema_errors, len(invalid))
            event.clear()
            stop, thread = start_watchers(Sink(), [path])
            try:
                self.assertTrue(event.wait(3))
                self.assertTrue(thread.is_alive())
            finally:
                stop.set()
                thread.join(3)

    def test_deep_session_header_and_invalid_activity_timestamps_are_isolated(self):
        reader = self.session('Codex')
        deep = b'{"nested":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}\n'
        good = self.codex('event_msg', type='task_started', turn_id='survivor')
        reader.path.write_bytes(deep + b'\n' * 512 + json.dumps(good).encode() + b'\n')
        with patch('observatory.local_agents.MAX_READ', 512):
            self.assertGreater(reader.poll(), 0)
        self.assertGreater(reader.schema_errors, 0)
        self.append(reader, [{**self.codex('turn_context', turn_id='bad'), 'timestamp': 10**100}, good])
        for value in (float('inf'), float('nan'), 1e300, 10**100, '9999-01-01T00:00:00Z'):
            self.assertEqual(timestamp(value), 0)

    def test_deep_opencode_part_does_not_hide_following_valid_tool(self):
        path = self.database()
        deep = '{"nested":' + '[' * 200 + '0' + ']' * 200 + '}'
        good = json.dumps({'type': 'tool', 'tool': 'survivor', 'state': {'status': 'completed'}})
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute('INSERT INTO part VALUES(?,?,?,?,?)', ('a-deep', 's', 'm', deep, 1))
            db.execute('INSERT INTO part VALUES(?,?,?,?,?)', ('b-good', 's', 'm', good, 1))
        reader = OpenCodeReader(self.store, path=path)
        self.assertGreater(reader.poll(), 0)
        self.assertEqual(reader.schema_errors, 1)
        self.assertEqual(reader.errors, 0)
        trace_id = identity('opencode-trace', 's', 'm')
        self.assertTrue(any(span['name'] == 'survivor' for span in self.store.trace(trace_id)['spans']))

    def test_oversized_message_is_not_loaded_even_in_metadata_only_mode(self):
        path = self.database()
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE message SET data=?', (json.dumps({"role": "assistant", "private": "x" * 4000}),))
        reader = OpenCodeReader(self.store, path=path, capture_details=False)
        with patch("observatory.opencode.MAX_PAYLOAD_BYTES", 1024), patch.object(reader, "_decode", wraps=reader._decode) as decode:
            self.assertEqual(reader.poll(), 0)
        decode.assert_not_called()
        self.assertEqual(len(reader.sessions), 1)
        self.assertEqual(reader.skipped_oversized, 1)
        self.assertEqual(reader.payload_bytes, 0)

    def test_schema_failure_is_content_free_and_retried_after_refresh(self):
        path = self.root / "broken.db"
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE session(unexpected TEXT)")
        reader = OpenCodeReader(self.store, path=path)
        reader.poll()
        self.assertEqual(reader.schema_errors, 1)
        self.assertEqual(reader.sessions, [])
        self.assertEqual(reader.diagnostics()["last_success_ns"], 0)
        with patch("observatory.opencode.time.monotonic", return_value=reader._last_refresh + 31):
            reader.poll()
        self.assertEqual(reader.schema_errors, 2)

    def test_session_message_role_column_and_missing_project_table(self):
        path = self.database()
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute('DROP TABLE project')
            db.execute('ALTER TABLE message RENAME TO session_message')
            db.execute('ALTER TABLE session_message ADD COLUMN type TEXT')
            db.execute('UPDATE session_message SET data=?,type=?', ('{"tokens":{"input":10}}', 'assistant'))
        reader = OpenCodeReader(self.store, path=path)
        self.assertGreater(reader.poll(), 0)
        self.assertEqual(reader.sessions[0]["project"], "fallback")
        self.assertEqual(reader.schema_errors, 0)
        self.assertEqual(self.store.sessions()[0]["input_tokens"], 10)

    def test_watcher_diagnostics_and_stderr_never_include_record_contents(self):
        path = self.root / "spans.jsonl"
        path.write_bytes(b'{"private":"DO_NOT_PRINT"}\n' + b'x' * 40 + b'\n')
        reader = JsonlWatcher(self.store, path)
        stderr = io.StringIO()
        with patch("observatory.watch.MAX_READ", 32), patch("observatory.watch.MAX_LINE", 32), contextlib.redirect_stderr(stderr):
            reader.poll()
            self.assertGreater(reader.backlog_bytes, 0)
            while reader.backlog_bytes:
                reader.poll()
        diagnostic = reader.diagnostics()
        self.assertGreater(diagnostic["last_success_ns"], 0)
        self.assertEqual(diagnostic["schema_errors"], 1)
        self.assertEqual(diagnostic["skipped_oversized_records"], 1)
        self.assertNotIn("DO_NOT_PRINT", stderr.getvalue() + json.dumps(diagnostic))


if __name__ == "__main__":
    unittest.main()
