"""Bounded local readers must preserve record boundaries across polls."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from observatory import local_agents, watch
from observatory.local_activity import claude_activity, codex_activity
from observatory.local_agents import LocalAgents, Session, timestamp


class LocalIngestionLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "events.jsonl"
        self.path.touch()
        self.store = Mock()
        self.store.ingest.side_effect = len

    def append(self, data):
        with self.path.open("ab") as stream:
            stream.write(data)

    def test_watcher_reads_only_one_budget_per_poll(self):
        self.path.write_bytes(b'{}\n' * 20)
        reader = watch.JsonlWatcher(self.store, self.path)
        with patch.object(watch, "MAX_READ", 12), patch.object(watch, "normalize_native", side_effect=lambda row, _: row):
            self.assertEqual(reader.poll(), 4)
            self.assertEqual(reader.offset, 12)
            self.assertEqual(reader.poll(), 4)
            self.assertEqual(reader.offset, 24)

    def test_watcher_discards_entire_oversized_line_not_its_json_suffix(self):
        reader = watch.JsonlWatcher(self.store, self.path)
        with patch.object(watch, "MAX_LINE", 16), patch.object(watch, "normalize_native", side_effect=lambda row, _: row):
            self.append(b"x" * 17)
            self.assertEqual(reader.poll(), 0)
            self.assertEqual(reader.pending, b"")
            self.append(b'{}\n{"valid":true}\n')
            self.assertEqual(reader.poll(), 1)
            self.assertEqual(self.store.ingest.call_args.args[0], [{"valid": True}])

    def test_session_discards_oversized_suffix_until_newline(self):
        reader = Session(self.store, self.path, "Codex")
        with patch.object(local_agents, "MAX_LINE", 16), patch.object(reader, "codex", return_value=[]) as parse:
            self.append(b"x" * 17)
            reader.poll()
            self.append(b"x" * 17)
            reader.poll()
            self.append(b'{}\n{"ok":true}\n')
            reader.poll()
        self.assertEqual(reader.skipped, 1)
        parse.assert_called_once_with({"ok": True}, 0)
        self.assertFalse(reader.discarding_line)

    def test_truncation_clears_discard_state_for_both_readers(self):
        for reader, module in ((watch.JsonlWatcher(self.store, self.path), watch),
                               (Session(self.store, self.path, "Codex"), local_agents)):
            with self.subTest(reader=type(reader).__name__), patch.object(module, "MAX_LINE", 16):
                self.path.write_bytes(b"x" * 17)
                reader.poll()
                self.assertTrue(reader.discarding_line)
                self.path.write_bytes(b"{}\n")
                reader.poll()
                self.assertFalse(reader.discarding_line)
                self.assertEqual(reader.pending, b"")

    def test_completed_tools_release_cached_inputs(self):
        for runtime, activity, call, result in (
            ("Codex", codex_activity,
             {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "call", "arguments": "{}"}},
             {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call", "output": "done"}}),
            ("Claude Code", claude_activity,
             {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "call", "name": "Bash"}]}},
             {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call", "content": "done"}]}}),
        ):
            with self.subTest(runtime=runtime):
                reader = Session(self.store, self.path, runtime)
                activity(reader, call, 1)
                self.assertIn("call", reader.tool_inputs)
                logs = activity(reader, result, 2)
                self.assertTrue(logs)
                self.assertEqual(reader.tool_inputs, {})

    def test_discovery_keeps_newest_sources_and_exact_omission_counts(self):
        codex = self.root / "codex"
        sessions = codex / "sessions"
        sessions.mkdir(parents=True)
        now = time.time()
        for index in range(6):
            path = sessions / f"{index}.jsonl"
            path.touch()
            os.utime(path, (now - index, now - index))
        observer = LocalAgents(self.store)
        with patch.dict(os.environ, {"CODEX_HOME": str(codex), "CLAUDE_CONFIG_DIR": str(self.root / "missing"),
                                     "PI_CODING_AGENT_DIR": str(self.root / "missing-pi")}), patch.object(local_agents, "MAX_SOURCES", 2):
            observer.discover()
        self.assertEqual(observer.discovered_sources, 6)
        self.assertEqual(observer.omitted_sources, 4)
        self.assertEqual({path.name for path in observer.sessions}, {"0.jsonl", "1.jsonl"})

    def test_nonfinite_timestamps_do_not_abort_session_ingestion(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            self.assertEqual(timestamp(value), 0)
        self.path.write_bytes(b'{"timestamp":NaN}\n{"timestamp":1}\n')
        reader = Session(self.store, self.path, "Codex")
        with patch.object(reader, "codex", return_value=[]) as parse:
            reader.poll()
        self.assertEqual(parse.call_count, 2)
        self.assertEqual(parse.call_args.args[1], 1_000_000_000)

    def test_truncated_session_resets_modern_usage_detection(self):
        self.path.write_bytes(b'{"type":"token_usage_record","payload":{"usage":{}}}\n')
        reader = Session(self.store, self.path, "Codex")
        reader.poll()
        self.assertTrue(reader.modern_usage)
        self.path.write_bytes(b"{}\n")
        reader.poll()
        self.assertFalse(reader.modern_usage)

    def test_watcher_survives_nonobject_json_and_nonfinite_span_times(self):
        span = {"trace_id": "1" * 32, "span_id": "2" * 16,
                "start_unix_nano": 1, "end_unix_nano": 2, "name": "valid"}
        invalid = dict(span, start_unix_nano=float("inf"))
        self.path.write_text("[]\n" + json.dumps(invalid) + "\n" + json.dumps(span) + "\n")
        reader = watch.JsonlWatcher(self.store, self.path)
        with patch("observatory.watch.sys.stderr"):
            self.assertEqual(reader.poll(), 1)
        self.assertEqual(self.store.ingest.call_args.args[0][0]["name"], "valid")
