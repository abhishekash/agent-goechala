import json
import io
import gzip
import sqlite3
import stat
import struct
import tempfile
import unittest
from pathlib import Path

from observatory.core import Store, normalize_native, normalize_otlp
from observatory.server import handler_for
from observatory.otlp_proto import decode_trace_request
from observatory.logs import decode_log_request, normalize_otlp_logs
from observatory.metrics import decode_metric_request, normalize_otlp_metrics
from observatory.watch import JsonlWatcher

TID = "a" * 32
ROOT = "1" * 16
CHILD = "2" * 16


def varint(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def pb_bytes(field, value):
    return varint((field << 3) | 2) + varint(len(value)) + value


def pb_fixed64(field, value):
    return varint((field << 3) | 1) + value.to_bytes(8, "little")


def pb_double(field, value):
    return varint((field << 3) | 1) + struct.pack("<d", value)


def pb_varint(field, value):
    return varint(field << 3) + varint(value)


def protobuf_fixture():
    def attr(key, value):
        any_value = pb_bytes(1, value.encode()) if isinstance(value, str) else pb_varint(3, value)
        return pb_bytes(1, key.encode()) + pb_bytes(2, any_value)
    resource = pb_bytes(1, attr("service.name", "protobuf-agent")) + pb_bytes(1, attr("deployment.environment.name", "test"))
    event = pb_fixed64(1, 150) + pb_bytes(2, b"progress") + pb_bytes(3, attr("step", 2))
    link = pb_bytes(1, bytes.fromhex("b" * 32)) + pb_bytes(2, bytes.fromhex(ROOT)) + pb_bytes(4, attr("edge", "handoff"))
    status = pb_varint(3, 2)
    span = (pb_bytes(1, bytes.fromhex(TID)) + pb_bytes(2, bytes.fromhex(CHILD)) +
            pb_bytes(4, bytes.fromhex(ROOT)) + pb_bytes(5, b"model.call") +
            pb_fixed64(7, 100) + pb_fixed64(8, 200) +
            pb_bytes(9, attr("gen_ai.usage.input_tokens", 42)) +
            pb_bytes(11, event) + pb_bytes(13, link) + pb_bytes(15, status))
    scope = pb_bytes(1, pb_bytes(1, b"agent.instrumentation")) + pb_bytes(2, span)
    return pb_bytes(1, pb_bytes(1, resource) + pb_bytes(2, scope))


def protobuf_log_fixture():
    def attr(key, value):
        any_value = pb_bytes(1, value.encode()) if isinstance(value, str) else pb_varint(3, value)
        return pb_bytes(1, key.encode()) + pb_bytes(2, any_value)
    record = (pb_fixed64(1, 150) + pb_varint(2, 9) + pb_bytes(3, b"INFO") +
              pb_bytes(5, pb_bytes(1, b"Found documents")) +
              pb_bytes(6, attr("agent.progress.stage", "retrieval")) +
              pb_bytes(6, attr("agent.progress.percent", 50)) +
              pb_bytes(9, bytes.fromhex(TID)) + pb_bytes(10, bytes.fromhex(ROOT)) +
              pb_bytes(12, b"agent.progress"))
    resource = pb_bytes(1, attr("service.name", "log-agent"))
    scope = pb_bytes(2, record)
    return pb_bytes(1, pb_bytes(1, resource) + pb_bytes(2, scope))


def protobuf_metric_fixture():
    resource_attr = pb_bytes(1, b"service.name") + pb_bytes(2, pb_bytes(1, b"metric-agent"))
    resource = pb_bytes(1, resource_attr)
    number = pb_fixed64(2, 100) + pb_fixed64(3, 200) + pb_fixed64(6, 123)
    metric = (pb_bytes(1, b"agent.tokens") + pb_bytes(3, b"{token}") +
              pb_bytes(7, pb_bytes(1, number) + pb_varint(2, 2)))
    histogram_point = pb_fixed64(3, 200) + pb_fixed64(4, 2) + pb_double(5, 1.5)
    histogram = pb_bytes(1, b"agent.latency") + pb_bytes(9, pb_bytes(1, histogram_point))
    gauge_point = pb_fixed64(3, 200) + pb_double(4, 0.75)
    gauge = pb_bytes(1, b"agent.progress") + pb_bytes(5, pb_bytes(1, gauge_point))
    exp_buckets = pb_varint(1, 2) + pb_varint(2, 2) + pb_varint(2, 1)
    exp_point = (pb_fixed64(3, 200) + pb_fixed64(4, 3) + pb_double(5, 2.5) +
                 pb_varint(6, 2) + pb_bytes(8, exp_buckets))
    exp = pb_bytes(1, b"agent.duration") + pb_bytes(10, pb_bytes(1, exp_point))
    quantile = pb_double(1, 0.5) + pb_double(2, 1.25)
    summary_point = pb_fixed64(3, 200) + pb_fixed64(4, 2) + pb_double(5, 2.5) + pb_bytes(6, quantile)
    summary = pb_bytes(1, b"agent.summary") + pb_bytes(11, pb_bytes(1, summary_point))
    scope = b"".join(pb_bytes(2, item) for item in (metric, histogram, gauge, exp, summary))
    return pb_bytes(1, pb_bytes(1, resource) + pb_bytes(2, scope))


class ObservatoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "runs.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_native_dedup_and_no_double_count(self):
        self.assertEqual(stat.S_IMODE(Path(self.store.path).stat().st_mode), 0o600)
        root = normalize_native({"trace_id": TID, "span_id": ROOT, "parent_span_id": None,
            "name": "agent.run", "start_unix_nano": 100, "end_unix_nano": 400,
            "attributes": {"agent.task": "Find answer", "agent.usage.input_tokens": 99}, "events": []})
        child = normalize_native({"trace_id": TID, "span_id": CHILD, "parent_span_id": ROOT,
            "name": "llm.complete", "start_unix_nano": 150, "end_unix_nano": 250,
            "attributes": {"llm.usage.input_tokens": 40, "llm.usage.output_tokens": 10,
                           "llm.cost_usd": 0.01, "llm.model": "demo"}, "events": []})
        self.store.ingest([root, child])
        self.store.ingest([child])
        run = self.store.traces()[0]
        self.assertEqual((run["span_count"], run["input_tokens"], run["output_tokens"]), (2, 40, 10))
        self.assertEqual(run["known_cost_usd"], 0.01)
        self.assertEqual(run["duration_ms"], 0.0)
        self.assertEqual(run["task"], "Find answer")

    def test_otlp_json_resource_and_semconv(self):
        body = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "langgraph"}}]},
            "scopeSpans": [{"scope": {"name": "langgraph.instrumentation"}, "spans": [{"traceId": TID.upper(), "spanId": CHILD,
                "name": "chat", "startTimeUnixNano": "1000000", "endTimeUnixNano": "3000000",
                "status": {"code": 2}, "attributes": [
                    {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "32"}},
                    {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "8"}},
                    {"key": "gen_ai.request.model", "value": {"stringValue": "model-x"}}],
                "links": [{"traceId": "b" * 32, "spanId": ROOT}]}]}]}]}
        self.store.ingest(normalize_otlp(body))
        run = self.store.traces(service="langgraph")[0]
        self.assertEqual((run["input_tokens"], run["output_tokens"], run["status"]), (32, 8, "ERROR"))
        self.assertFalse(run["cost_complete"])
        self.assertEqual(run["models"], ["model-x"])
        self.assertEqual(self.store.trace(TID)["spans"][0]["links"][0]["trace_id"], "b" * 32)

    def test_otlp_protobuf_resource_attributes_events_and_malformed(self):
        spans = decode_trace_request(protobuf_fixture())
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["service"], "protobuf-agent")
        self.assertEqual(spans[0]["parent_span_id"], ROOT)
        self.assertEqual(spans[0]["attributes"]["gen_ai.usage.input_tokens"], 42)
        self.assertEqual(spans[0]["events"][0]["attributes"]["step"], 2)
        self.assertEqual(spans[0]["links"][0]["trace_id"], "b" * 32)
        self.assertEqual(spans[0]["links"][0]["attributes"]["edge"], "handoff")
        self.assertEqual(spans[0]["resource_attributes"]["deployment.environment.name"], "test")
        self.assertEqual(spans[0]["scope_name"], "agent.instrumentation")
        self.assertEqual(spans[0]["status"], "ERROR")
        self.store.ingest(spans)
        self.assertEqual(self.store.traces()[0]["input_tokens"], 42)
        self.assertEqual(self.store.trace(TID)["spans"][0]["links"][0]["trace_id"], "b" * 32)
        with self.assertRaises(ValueError):
            decode_trace_request(protobuf_fixture()[:-1])

    def test_budget_stop_is_incomplete_not_success(self):
        span = normalize_native({"trace_id": TID, "span_id": ROOT,
            "name": "agent.run", "start_unix_nano": 100, "end_unix_nano": 200,
            "attributes": {"agent.stopped_reason": "max_steps", "agent.steps": 4}, "events": []})
        self.store.ingest([span])
        run = self.store.traces()[0]
        self.assertEqual((run["status"], run["outcome"], self.store.metrics()["incomplete"]),
                         ("INCOMPLETE", "max_steps", 1))

    def test_harness_tool_error_is_visible_when_span_status_unset(self):
        span = normalize_native({"trace_id": TID, "span_id": CHILD,
            "name": "tool.call", "start_unix_nano": 100, "end_unix_nano": 200,
            "status": "UNSET", "attributes": {"tool.name": "retrieve",
                "tool.result_status": "error", "tool.error_message": "timeout"}, "events": []})
        self.store.ingest([span])
        self.assertEqual(self.store.traces(status="ERROR")[0]["trace_id"], TID)
        self.assertEqual(self.store.trace(TID)["findings"][0]["evidence"][0]["id"], CHILD)

    def test_repeated_tool_failures_have_source_span_evidence(self):
        spans = []
        for index, span_id in enumerate((ROOT, CHILD)):
            spans.append(normalize_native({"trace_id": TID, "span_id": span_id,
                "name": "tool.call", "start_unix_nano": 100 + index,
                "end_unix_nano": 200 + index, "status": "UNSET",
                "attributes": {"tool.name": "search", "tool.error_message": "timeout",
                               "tool.result_status": "error"}, "events": []}))
        self.store.ingest(spans)
        repeated = [f for f in self.store.findings() if f["kind"] == "repeated_tool_error"]
        self.assertEqual(len(repeated), 1)
        self.assertEqual({e["id"] for e in repeated[0]["evidence"]}, {ROOT, CHILD})

    def test_failed_evaluation_overrides_clean_execution_status(self):
        span = normalize_native({"trace_id": TID, "span_id": ROOT, "name": "agent.run",
            "start_unix_nano": 100, "end_unix_nano": 200,
            "attributes": {"agent.stopped_reason": "completed"}, "events": []})
        self.store.ingest([span])
        self.store.ingest_outcomes([{"trace_id": TID, "task_id": "retrieval-loop-v1",
            "passed": False, "scores": [{"scorer": "answer_regex", "passed": False,
            "detail": "wrong key"}]}], source="results.json")
        self.assertEqual(self.store.traces()[0]["status"], "FAILED")
        self.assertEqual(self.store.metrics()["failed_outcomes"], 1)
        detail = self.store.trace(TID)
        self.assertEqual(detail["outcome"]["task_id"], "retrieval-loop-v1")
        self.assertEqual(detail["findings"][0]["kind"], "failed_outcome")
        self.assertEqual(detail["findings"][0]["evidence"][0]["id"], TID)

    def test_progress_only_run_is_visible(self):
        self.store.add_progress(TID, "planning", "Choosing tools", 10)
        self.assertEqual(self.store.traces()[0]["status"], "RUNNING")
        self.assertEqual(self.store.metrics()["running"], 1)
        detail = self.store.trace(TID)
        self.assertEqual(detail["summary"]["span_count"], 0)
        self.assertEqual(detail["progress"][0]["message"], "Choosing tools")

    def test_existing_database_is_migrated_for_otlp_context(self):
        path = Path(self.temp.name) / "legacy.db"
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE spans(trace_id TEXT,span_id TEXT,parent_span_id TEXT,
                name TEXT,start_ns INTEGER,end_ns INTEGER,status TEXT,service TEXT,
                attributes TEXT,events TEXT,PRIMARY KEY(trace_id,span_id))""")
        migrated = Store(path)
        migrated.ingest(decode_trace_request(protobuf_fixture()))
        span = migrated.trace(TID)["spans"][0]
        self.assertEqual(span["scope_name"], "agent.instrumentation")
        self.assertEqual(span["links"][0]["attributes"]["edge"], "handoff")

    def test_related_traces_follow_span_links_in_both_directions(self):
        self.store.ingest(decode_trace_request(protobuf_fixture()))
        other = normalize_native({"trace_id": "b" * 32, "span_id": ROOT,
            "name": "child-agent", "start_unix_nano": 110, "end_unix_nano": 210,
            "attributes": {}, "events": []}, service="child-agent")
        self.store.ingest([other])
        outgoing = self.store.related_traces(TID)
        incoming = self.store.related_traces("b" * 32)
        self.assertEqual((outgoing[0]["direction"], outgoing[0]["summary"]["services"]),
                         ("outgoing", ["child-agent"]))
        self.assertEqual((incoming[0]["direction"], incoming[0]["trace_id"]),
                         ("incoming", TID))

    def test_otlp_logs_json_and_protobuf_link_progress_to_trace(self):
        json_body = {"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "json-agent"}}]},
            "scopeLogs": [{"logRecords": [{"timeUnixNano": "140", "traceId": TID,
                "severityNumber": 17, "severityText": "ERROR", "body": {"stringValue": "retrieval failed"}}]}]}]}
        self.store.ingest_logs(normalize_otlp_logs(json_body))
        proto = decode_log_request(protobuf_log_fixture())
        self.assertEqual(proto[0]["body"], "Found documents")
        self.assertEqual(proto[0]["attributes"]["agent.progress.percent"], 50)
        self.store.ingest_logs(proto)
        self.store.ingest_logs(proto)
        trace = self.store.trace(TID)
        self.assertEqual(len(trace["logs"]), 2)
        self.assertEqual(trace["progress"][0]["stage"], "retrieval")
        self.assertEqual(trace["summary"]["status"], "RUNNING")
        self.assertEqual(trace["summary"]["services"], ["json-agent", "log-agent"])
        self.assertEqual(self.store.traces()[0]["services"], ["json-agent", "log-agent"])
        self.assertEqual(self.store.metrics()["logs"], 2)
        self.assertEqual(len([f for f in trace["findings"] if f["kind"] == "error_log"]), 1)

    def test_otlp_metric_points_json_and_protobuf_are_typed(self):
        body = {"resourceMetrics": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "json-metrics"}}]},
            "scopeMetrics": [{"metrics": [{"name": "agent.queue.depth", "unit": "{item}",
                "gauge": {"dataPoints": [{"timeUnixNano": "300", "asInt": "4",
                    "attributes": [{"key": "queue", "value": {"stringValue": "work"}}]}]}}]}]}]}
        self.store.ingest_metrics(normalize_otlp_metrics(body))
        decoded = decode_metric_request(protobuf_metric_fixture())
        self.assertEqual({p["kind"] for p in decoded},
                         {"sum", "histogram", "gauge", "exponentialHistogram", "summary"})
        self.store.ingest_metrics(decoded)
        self.store.ingest_metrics(decoded)
        points = self.store.metric_points()
        self.assertEqual(len(points), 6)
        self.assertEqual(next(p for p in points if p["name"] == "agent.tokens")["data"]["value"], 123)
        self.assertEqual(next(p for p in points if p["name"] == "agent.latency")["data"]["count"], 2)
        self.assertEqual(next(p for p in points if p["name"] == "agent.duration")["data"]["positive"]["bucketCounts"], [2, 1])
        self.assertEqual(next(p for p in points if p["name"] == "agent.summary")["data"]["quantileValues"][0]["value"], 1.25)
        self.assertEqual(self.store.metric_points(service="json-metrics")[0]["attributes"]["queue"], "work")
        self.assertEqual(self.store.metrics()["metric_points"], 6)
        self.assertEqual(self.store.metrics()["services"], ["json-metrics", "metric-agent"])
        with self.assertRaises(ValueError):
            normalize_otlp_metrics({"resourceMetrics": [{"scopeMetrics": [
                {"metrics": [{"name": "unknown.metric", "futureKind": {"dataPoints": [{}]}}]}]}]})
        nonfinite = normalize_otlp_metrics({"resourceMetrics": [{"scopeMetrics": [
            {"metrics": [{"name": "nan.metric", "gauge": {"dataPoints": [
                {"timeUnixNano": "100", "asDouble": float("nan")} ]}}]}]}]})
        self.assertEqual(nonfinite[0]["data"]["value"], "nan")

    def test_http_ingest_query_progress_and_token(self):
        class Socket:
            def __init__(self, request):
                self.input = io.BytesIO(request)
                self.output = io.BytesIO()
            def makefile(self, mode, *args, **kwargs):
                return self.input if "r" in mode else self.output
            def sendall(self, data):
                self.output.write(data)
        class Server:
            server_name = "localhost"
            server_port = 4318
        def request(path, body=None, token="secret", content_type="application/json", encoding=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
            headers = f"Content-Type: {content_type}\r\n"
            if encoding:
                headers += f"Content-Encoding: {encoding}\r\n"
            if token:
                headers += f"Authorization: Bearer {token}\r\n"
            if data is not None:
                headers += f"Content-Length: {len(data)}\r\n"
            raw = f"{'POST' if data is not None else 'GET'} {path} HTTP/1.1\r\n{headers}\r\n".encode() + (data or b"")
            sock = Socket(raw)
            handler_for(self.store, "secret")(sock, ("127.0.0.1", 1234), Server())
            head, payload = sock.output.getvalue().split(b"\r\n\r\n", 1)
            return int(head.split()[1]), json.loads(payload) if payload and path != "/" else payload.decode()
        self.assertEqual(request("/api/metrics", token=None)[0], 401)
        self.assertEqual(request("/", token=None)[0], 200)
        span = {"trace_id": TID, "span_id": ROOT, "name": "agent.run",
                "start_unix_nano": 10, "end_unix_nano": 20, "attributes": {}, "events": []}
        self.assertEqual(request("/api/spans", {"service": "custom-agent", "spans": [span]})[1]["accepted"], 1)
        request("/api/progress", {"trace_id": TID, "stage": "retrieving", "message": "2 docs", "percent": 50})
        self.assertEqual(request("/api/traces")[1]["traces"][0]["services"], ["custom-agent"])
        self.assertEqual(request("/api/traces/" + TID)[1]["progress"][0]["stage"], "retrieving")
        otlp = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "other-agent"}}]},
                "scopeSpans": [{"spans": [{"traceId": "b" * 32, "spanId": CHILD,
                    "name": "model", "startTimeUnixNano": "10", "endTimeUnixNano": "20"}]}]}]}
        self.assertEqual(request("/v1/traces", otlp)[1]["partialSuccess"]["rejectedSpans"], 0)
        self.assertEqual(request("/v1/traces", protobuf_fixture(), content_type="application/x-protobuf"), (200, ""))
        self.assertEqual(request("/v1/traces", gzip.compress(protobuf_fixture()),
                                 content_type="application/x-protobuf", encoding="gzip"), (200, ""))
        self.assertEqual(request("/v1/logs", protobuf_log_fixture(), content_type="application/x-protobuf"), (200, ""))
        self.assertEqual(request("/v1/metrics", protobuf_metric_fixture(), content_type="application/x-protobuf"), (200, ""))
        self.assertEqual(len(request("/api/measurements")[1]["points"]), 5)
        self.assertEqual(request("/api/traces/" + TID)[1]["logs"][0]["body"], "Found documents")
        self.assertTrue(request("/api/findings")[1]["findings"])
        self.assertEqual(request("/api/traces/" + TID + "/related")[1]["related"][0]["trace_id"], "b" * 32)
        outcome = {"trace_id": TID, "task_id": "demo", "passed": False,
                   "scores": [{"scorer": "answer_regex", "passed": False}]}
        self.assertEqual(request("/api/outcomes", outcome)[1]["accepted"], 1)
        self.assertEqual(request("/api/traces/" + TID)[1]["outcome"]["passed"], False)
        self.assertEqual(request("/v1/traces", b"not a protobuf", content_type="text/plain")[0], 415)
        self.assertEqual(request("/v1/traces", b"bad gzip", content_type="application/x-protobuf", encoding="gzip")[0], 400)
        self.assertEqual(self.store.metrics()["runs"], 2)

    def test_watcher_keeps_partial_line_and_reimports_after_rotation(self):
        path = Path(self.temp.name) / "active.jsonl"
        watcher = JsonlWatcher(self.store, path)
        self.assertEqual(watcher.poll(), 0)
        span = {"trace_id": TID, "span_id": ROOT, "name": "agent.run",
                "start_unix_nano": 10, "end_unix_nano": 20, "attributes": {}, "events": []}
        encoded = json.dumps(span).encode()
        path.write_bytes(encoded[:20])
        self.assertEqual(watcher.poll(), 0)
        with path.open("ab") as stream:
            stream.write(encoded[20:] + b"\n")
        self.assertEqual(watcher.poll(), 1)
        self.assertEqual(watcher.poll(), 0)
        replacement = Path(self.temp.name) / "replacement.jsonl"
        replacement.write_bytes(encoded + b"\n")
        replacement.replace(path)
        self.assertEqual(watcher.poll(), 1)
        self.assertEqual(self.store.metrics()["spans"], 1)


if __name__ == "__main__":
    unittest.main()
