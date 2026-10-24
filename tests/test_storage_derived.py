"""Exact aggregates, bounded evidence, and SQLite-wide retention ordering."""
import concurrent.futures
import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import tracemalloc
import unittest
from unittest.mock import patch

from observatory.core import Store, normalize_native, summarize
from observatory.logs import normalize_otlp_logs
from observatory.storage_derived import EVIDENCE_LIMIT, GROUP_LIMIT, RECORD_BYTES, WINDOW_BYTES, derive_trace


TID = "c" * 32
DAY = 86_400_000_000_000


def span(number, parent=None, attrs=None, name="model", status="OK"):
    return normalize_native({"trace_id": TID, "span_id": f"{number:016x}",
        "parent_span_id": f"{parent:016x}" if parent else None,
        "name": name, "start_unix_nano": number * 100, "end_unix_nano": number * 100 + 50,
        "attributes": attrs or {}, "status": status})


def prune_in_process(path, ready, start, done):
    store = Store(path)
    ready.set()
    if not start.wait(10):
        raise RuntimeError("test start timeout")
    store.prune(1, now_ns=2 * DAY)
    store.close()
    done.set()


class DerivedStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "telemetry.db"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def cached(self):
        with self.store._connect() as db:
            row = db.execute("SELECT summary_json FROM trace_summaries WHERE trace_id=?", (TID,)).fetchone()
        return json.loads(row[0]) if row else None

    def assert_empty(self):
        with self.store._connect() as db:
            for table in ("spans", "trace_activity", "trace_summaries", "finding_cache", "derived_span_stats_v1"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_large_trace_totals_are_not_evidence_window_totals(self):
        records = [span(1, attrs={"agent.task": "original task"}, name="agent.run")]
        records += [span(n, 1, {"gen_ai.usage.input_tokens": 3,
                                "gen_ai.usage.output_tokens": 2,
                                "llm.cost.total": 0.01,
                                "session.id": "old-identity"} if n == 2 else
                         {"gen_ai.usage.input_tokens": 3, "gen_ai.usage.output_tokens": 2,
                          "llm.cost.total": 0.01}) for n in range(2, 1002)]
        records[1]["status"] = "ERROR"  # Outside the newest evidence window.
        with patch.object(self.store, "trace", side_effect=AssertionError("full trace read")), \
             patch.object(self.store, "_finding_logs", side_effect=AssertionError("full log read")):
            self.store.ingest(records)
        summary = self.cached()
        self.assertEqual(summary["span_count"], 1001)
        self.assertEqual(summary["input_tokens"], 3000)
        self.assertEqual(summary["output_tokens"], 2000)
        self.assertEqual(summary["known_cost_usd"], 10)
        self.assertTrue(summary["cost_complete"])
        self.assertEqual(summary["status"], "ERROR")
        self.assertEqual(summary["session_id"], "old-identity")
        self.assertEqual(summary["task"], "original task")
        self.assertEqual(summary["start_unix_nano"], 100)
        self.assertEqual(summary["end_unix_nano"], 100150)
        self.assertLessEqual(summary["evidence_window"]["spans_returned"], EVIDENCE_LIMIT + 1)
        self.assertFalse(summary["findings_complete"])

    def test_sql_usage_matches_reference_with_wrappers_and_upserts(self):
        records = [span(1, name="agent.run"),
            span(2, 1, {"gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 200,
                        "llm.cost.total": 5}),
            span(3, 2),  # Usage-free intermediate wrapper.
            span(4, 3, {"gen_ai.usage.input_tokens": 10, "llm.cost.total": 1}),
            span(5, 2, {"gen_ai.usage.output_tokens": 20}),
            span(6, 1, {"gen_ai.usage.input_tokens": "invalid", "llm.cost.prompt": 0.5}),
            span(7, 1, {"gen_ai.usage.cache_read.input_tokens": 50,
                        "gen_ai.usage.reasoning.output_tokens": 7})]
        for changed in (None, {"gen_ai.usage.input_tokens": 30, "gen_ai.usage.output_tokens": 40,
                               "llm.cost.total": 2}):
            if changed:
                records[3]["attributes"] = changed
            self.store.ingest(records)
            expected = summarize(records)
            actual = self.cached()
            for field in ("span_count", "input_tokens", "output_tokens", "input_tokens_reported",
                          "output_tokens_reported", "cache_read_input_tokens", "reasoning_output_tokens",
                          "known_cost_usd", "cost_complete", "status", "active", "models", "services"):
                self.assertEqual(actual[field], expected[field], field)

    def test_large_logs_exact_bounds_and_explicit_partial_findings(self):
        records = normalize_otlp_logs({"resourceLogs": [{"scopeLogs": [{"logRecords": [
            {"traceId": TID, "timeUnixNano": n * 100, "severityNumber": 17,
             "body": {"stringValue": f"error {n}"}} for n in range(1, 1002)]}]}]})
        self.store.ingest_logs(records)
        summary = self.cached()
        self.assertEqual(summary["start_unix_nano"], 100)
        self.assertEqual(summary["end_unix_nano"], 100100)
        self.assertEqual(summary["evidence_window"]["total_logs"], 1001)
        self.assertEqual(summary["evidence_window"]["logs_returned"], EVIDENCE_LIMIT)
        findings = self.store.findings(limit=500)
        self.assertEqual(len(findings), EVIDENCE_LIMIT)
        self.assertTrue(all(not finding["evidence_complete"] for finding in findings))
        self.assertTrue(all("Bounded evidence window" in finding["detail"] for finding in findings))

    def test_statistics_cache_only_recalculates_changed_spans(self):
        records = [span(1, name="agent.run"), span(2, 1, {"gen_ai.usage.input_tokens": 10}),
                   span(3, 1, {"gen_ai.usage.input_tokens": 20})]
        self.store.ingest(records)
        with self.store._connect() as db:
            db.execute("CREATE TABLE stats_audit(span_id TEXT)")
            db.execute("""CREATE TRIGGER audit_stats AFTER INSERT ON derived_span_stats_v1
                BEGIN INSERT INTO stats_audit VALUES(NEW.span_id); END""")
        self.store.ingest(records)  # Idempotent source upsert must reuse all stats.
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM stats_audit").fetchone()[0], 0)
        records[1]["attributes"]["gen_ai.usage.input_tokens"] = 50
        self.store.ingest(records)
        self.assertEqual(self.cached()["input_tokens"], 70)
        with self.store._connect() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT span_id FROM stats_audit")],
                             [records[1]["span_id"]])

    def test_oversized_usage_payload_is_unknown_not_a_zero_total(self):
        self.store.ingest([span(1, attrs={"gen_ai.usage.input_tokens": 123,
            "session.id": "budget-session", "gen_ai.request.model": "outside-budget",
            "captured.text": "x" * RECORD_BYTES})])
        summary = self.cached()
        self.assertIsNone(summary["input_tokens"])
        self.assertIsNone(summary["known_cost_usd"])
        self.assertIsNone(summary["input_tokens_reported"])
        self.assertFalse(summary["usage_complete"])
        self.assertFalse(summary["models_complete"])
        self.assertFalse(summary["status_complete"])
        self.assertFalse(summary["evidence_window"]["complete"])
        self.assertEqual(summary["status"], "UNKNOWN")
        self.assertIsNone(self.store.metrics()["input_tokens"])
        self.assertFalse(self.store.metrics()["usage_complete"])
        session = self.store.sessions()[0]
        self.assertIsNone(session["input_tokens"])
        self.assertFalse(session["usage_complete"])
        with patch("observatory.core.time.time_ns", return_value=DAY // 2):
            buckets = self.store.usage_series(days=1)["points"]
        used = next(bucket for bucket in buckets if bucket["runs"])
        self.assertIsNone(used["input_tokens"])
        self.assertFalse(used["usage_complete"])
        self.store.ingest([span(1, attrs={"gen_ai.usage.input_tokens": 123})])
        self.assertEqual(self.cached()["input_tokens"], 123)
        self.assertTrue(self.cached()["usage_complete"])

    def test_group_cardinality_and_labels_are_bounded_and_explicit(self):
        records = [span(n, attrs={"gen_ai.request.model": f"model-{n:04}"})
                   for n in range(1, GROUP_LIMIT + 10)]
        for index, record in enumerate(records):
            record["service"] = f"service-{index:04}"
        self.store.ingest(records)
        summary = self.cached()
        self.assertEqual(len(summary["services"]), GROUP_LIMIT)
        self.assertEqual(len(summary["models"]), GROUP_LIMIT)
        self.assertFalse(summary["services_complete"])
        self.assertFalse(summary["models_complete"])
        self.assertTrue(summary["usage_complete"])
        record = span(1, attrs={"gen_ai.request.model": "m" * 1000})
        record["service"] = "s" * 1000
        self.store.prune(1, now_ns=2 * DAY)
        self.store.ingest([record])
        summary = self.cached()
        self.assertLessEqual(len(summary["services"][0]), 128)
        self.assertLessEqual(len(summary["models"][0]), 128)
        self.assertFalse(summary["services_complete"])
        self.assertFalse(summary["models_complete"])

    def test_evidence_has_a_cumulative_byte_budget(self):
        records = [span(n, attrs={"captured.text": "x" * (RECORD_BYTES // 2)})
                   for n in range(1, 200)]
        self.store.ingest(records)
        summary = self.cached()
        self.assertLess(summary["evidence_window"]["spans_returned"], EVIDENCE_LIMIT)
        self.assertLessEqual(summary["evidence_window"]["spans_returned"],
                             WINDOW_BYTES // (RECORD_BYTES // 2) + 1)
        self.assertFalse(summary["evidence_window"]["complete"])
        self.assertTrue(summary["usage_complete"])

    def test_refresh_python_allocations_do_not_scale_with_raw_trace_bytes(self):
        # Populate without retaining decoded span objects in the measured phase.
        attrs = json.dumps({"captured.text": "x" * 8192, "gen_ai.usage.input_tokens": 2})
        with self.store._connect() as db:
            db.executemany("""INSERT INTO spans(trace_id,span_id,parent_span_id,name,start_ns,end_ns,
                status,service,attributes,events) VALUES(?,?,NULL,'model',?,?, 'OK','test',?,'[]')""",
                ((TID, f"{n:016x}", n, n + 1, attrs) for n in range(1, 2001)))
        tracemalloc.start()
        try:
            with patch("observatory.storage_derived.EVIDENCE_LIMIT", 16):
                self.store._refresh_summaries([TID])
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        # Raw attributes alone exceed 16 MiB. This is a Python allocation
        # regression, not an assertion about SQLite's cache or whole-process RSS.
        self.assertLess(peak, 4 * 1024 * 1024)
        self.assertEqual(self.cached()["input_tokens"], 4000)

    def test_late_child_and_reparenting_recompute_usage_leaves(self):
        self.store.ingest([span(1, name="agent.run"),
                           span(2, 1, {"gen_ai.usage.input_tokens": 100})])
        self.assertEqual(self.cached()["input_tokens"], 100)
        child = span(3, 2, {"gen_ai.usage.input_tokens": 20})
        self.store.ingest([child])
        self.assertEqual(self.cached()["input_tokens"], 20)
        child["parent_span_id"] = f"{1:016x}"
        self.store.ingest([child])
        self.assertEqual(self.cached()["input_tokens"], 120)

    def test_deep_ancestry_and_cycles_match_reference(self):
        records = [span(1, name="agent.run")]
        records.extend(span(n, n - 1, {"gen_ai.usage.input_tokens": n}) for n in range(2, 2002))
        self.store.ingest(records)
        self.assertEqual(self.cached()["input_tokens"], 2001)
        cycle = [span(1, 2, {"gen_ai.usage.input_tokens": 10}),
                 span(2, 1, {"gen_ai.usage.input_tokens": 20})]
        self.store.prune(1, now_ns=2 * DAY)
        self.store.ingest(cycle)
        self.assertEqual(self.cached()["input_tokens"], summarize(cycle)["input_tokens"])

    def test_refresh_after_other_store_prunes_does_not_recreate_cache(self):
        self.store.ingest([span(1, status="ERROR")])
        other = Store(self.path)
        other.prune(1, now_ns=2 * DAY)
        self.store._refresh_summaries([TID])
        self.assert_empty()
        other.close()

    def test_other_store_prune_waits_for_atomic_refresh(self):
        self.store.ingest([span(1, status="ERROR")])
        other = Store(self.path)
        read = threading.Event()
        release = threading.Event()

        def paused(db, trace_id):
            result = derive_trace(db, trace_id)
            read.set()
            if not release.wait(5):
                raise RuntimeError("release timeout")
            return result

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            with patch("observatory.storage_derived.derive_trace", side_effect=paused):
                refresh = pool.submit(self.store._refresh_summaries, [TID])
                try:
                    self.assertTrue(read.wait(5))
                    prune = pool.submit(other.prune, 1, now_ns=2 * DAY)
                    with self.assertRaises(concurrent.futures.TimeoutError):
                        prune.result(timeout=0.1)
                finally:
                    release.set()
                refresh.result(timeout=5)
                prune.result(timeout=5)
        self.assert_empty()
        other.close()

    def test_other_process_prune_waits_for_atomic_refresh(self):
        self.store.ingest([span(1, status="ERROR")])
        context = multiprocessing.get_context("spawn")
        ready, start, done = (context.Event() for _ in range(3))
        process = context.Process(target=prune_in_process, args=(self.path, ready, start, done))
        process.start()
        release = threading.Event()
        read = threading.Event()

        def paused(db, trace_id):
            result = derive_trace(db, trace_id)
            read.set()
            if not release.wait(5):
                raise RuntimeError("release timeout")
            return result

        try:
            self.assertTrue(ready.wait(10))
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                with patch("observatory.storage_derived.derive_trace", side_effect=paused):
                    refresh = pool.submit(self.store._refresh_summaries, [TID])
                    try:
                        self.assertTrue(read.wait(5))
                        start.set()
                        self.assertFalse(done.wait(0.2))
                    finally:
                        release.set()
                    refresh.result(timeout=5)
            process.join(10)
            self.assertEqual(process.exitcode, 0)
            self.assertTrue(done.is_set())
            self.assert_empty()
        finally:
            release.set()
            start.set()
            if process.is_alive():
                process.terminate()
            process.join(5)

    def test_failed_refresh_rolls_back_previous_cache(self):
        self.store.ingest([span(1)])
        before = self.cached()
        with patch("observatory.storage_derived.derive_trace", side_effect=RuntimeError("failure")):
            with self.assertRaisesRegex(RuntimeError, "failure"):
                self.store._refresh_summaries([TID])
        self.assertEqual(before, self.cached())
