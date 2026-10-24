"""Regression coverage for receiver/storage reliability and privacy boundaries."""
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from observatory.core import Store, normalize_native
from observatory.logs import normalize_otlp_logs
from observatory.metrics import normalize_otlp_metrics
from observatory.server import handler_for
from observatory.storage_derived import derive_trace


TID = "a" * 32


def span(number=1, attributes=None):
    return normalize_native({"trace_id": TID, "span_id": f"{number:016x}",
                             "name": "tool", "start_unix_nano": 100,
                             "end_unix_nano": 200, "attributes": attributes or {}})


def metric_body(timestamp=200, start=100):
    return {"resourceMetrics": [{"resource": {"attributes": [
        {"key": "host.name", "value": {"stringValue": "test-host"}}]},
        "scopeMetrics": [{"metrics": [{"name": "load", "gauge": {
            "dataPoints": [{"timeUnixNano": timestamp,
                            "startTimeUnixNano": start, "asDouble": 0.5}]}}]}]}]}


class StorageHardeningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "telemetry.db"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_plain_text_tool_results_do_not_break_error_filter(self):
        records = [span(1, {"gen_ai.tool.call.result": "plain text, not JSON"}),
                   span(2, {"gcp.vertex.agent.tool_response": '{"isError": true}'}),
                   span(3, {"gen_ai.tool.call.result": {"is_error": True}}),
                   span(4, {"gen_ai.tool.call.result": 42})]
        self.store.ingest(records)
        page = self.store.span_page(TID, span_filter="errors")
        self.assertEqual({item["span_id"] for item in page["spans"]},
                         {f"{2:016x}", f"{3:016x}"})

    def test_metric_generator_updates_endpoint_inventory(self):
        points = normalize_otlp_metrics(metric_body())
        self.assertEqual(self.store.ingest_metrics(iter(points)), 1)
        self.assertEqual(self.store.endpoints()[0]["name"], "test-host")
        self.assertEqual(len(self.store.metric_points()), 1)

    def test_retention_cannot_be_undone_by_inflight_refresh(self):
        self.store.ingest([span()])
        read_complete = threading.Event()
        allow_refresh = threading.Event()
        prune_started = threading.Event()
        prune_finished = threading.Event()
        failures = []
        def paused_derive(db, trace_id):
            result = derive_trace(db, trace_id)
            read_complete.set()
            if not allow_refresh.wait(5):
                raise RuntimeError("refresh test timed out")
            return result

        def refresh():
            try:
                self.store._refresh_summaries([TID])
            except Exception as exc:
                failures.append(exc)

        def prune():
            prune_started.set()
            try:
                self.store.prune(1, now_ns=2 * 86_400_000_000_000)
            except Exception as exc:
                failures.append(exc)
            finally:
                prune_finished.set()

        with patch("observatory.storage_derived.derive_trace", side_effect=paused_derive):
            refresher = threading.Thread(target=refresh)
            pruner = threading.Thread(target=prune)
            refresher.start()
            try:
                self.assertTrue(read_complete.wait(5))
                pruner.start()
                self.assertTrue(prune_started.wait(5))
                self.assertFalse(prune_finished.wait(0.1))
            finally:
                allow_refresh.set()
                refresher.join(5)
                if pruner.ident is not None:
                    pruner.join(5)
        self.assertFalse(failures)
        self.assertFalse(refresher.is_alive())
        self.assertFalse(pruner.is_alive())
        with self.store._connect() as db:
            for table in ("spans", "trace_activity", "trace_summaries", "finding_cache"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_database_is_private_before_first_sqlite_connection(self):
        target = Path(self.directory.name) / "new.db"
        original_connect = Store._connect
        observed_modes = []

        def inspect_connect(store):
            observed_modes.append(stat.S_IMODE(os.stat(store.path).st_mode))
            return original_connect(store)

        with patch.object(Store, "_connect", inspect_connect):
            other = Store(target)
            other.close()
        self.assertTrue(observed_modes)
        self.assertEqual(set(observed_modes), {0o600})

    def test_span_order_query_uses_covering_order_index(self):
        with self.store._connect() as db:
            plan = db.execute("EXPLAIN QUERY PLAN SELECT * FROM spans WHERE trace_id=? "
                              "ORDER BY start_ns,span_id LIMIT 100", (TID,)).fetchall()
        detail = " ".join(row[3] for row in plan)
        self.assertIn("spans_trace_start", detail)
        self.assertNotIn("TEMP B-TREE", detail)

    def test_receiver_returns_400_for_overflow_and_excessive_json_nesting(self):
        class Socket:
            def __init__(self, request):
                self.input = io.BytesIO(request)
                self.output = io.BytesIO()

            def makefile(self, mode, *args, **kwargs):
                return self.input

            def sendall(self, data):
                self.output.write(data)

        body = json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": [
            {"timeUnixNano": "200", "severityNumber": float("inf")}]}]}]}).encode()
        for payload in (body, b"[" * 2000 + b"]" * 2000):
            with self.subTest(payload_size=len(payload)):
                request = (b"POST /v1/logs HTTP/1.1\r\nContent-Type: application/json\r\n"
                           + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
                sock = Socket(request)
                handler_for(self.store, quiet=True)(sock, ("127.0.0.1", 1234), object())
                self.assertEqual(sock.output.getvalue().split()[1], b"400")
        self.assertEqual(self.store.logs(), [])


class TimestampValidationTests(unittest.TestCase):
    def test_span_rejects_sqlite_timestamp_overflow(self):
        record = span()
        record["end_unix_nano"] = 2**63
        with self.assertRaises(ValueError):
            normalize_native(record)

    def test_logs_reject_sqlite_timestamp_overflow(self):
        with self.assertRaises(ValueError):
            normalize_otlp_logs({"resourceLogs": [{"scopeLogs": [{
                "logRecords": [{"timeUnixNano": str(2**63)}]}]}]})

    def test_metrics_reject_invalid_timestamp_ranges(self):
        for timestamp, start in ((2**63, 100), (200, -1), (200, 201)):
            with self.subTest(timestamp=timestamp, start=start):
                with self.assertRaises(ValueError):
                    normalize_otlp_metrics(metric_body(timestamp, start))


if __name__ == "__main__":
    unittest.main()
