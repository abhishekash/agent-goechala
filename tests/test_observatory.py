import json
import io
import gzip
import sqlite3
import stat
import struct
import tempfile
import time
import unittest
from pathlib import Path

from observatory.core import Store, normalize_native, normalize_otlp
from observatory.server import handler_for
from observatory.otlp_proto import decode_trace_request
from observatory.logs import decode_log_request, normalize_otlp_logs
from observatory.metrics import decode_metric_request, normalize_otlp_metrics
from observatory.terminal_ui import metric_value
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
              pb_bytes(7, pb_bytes(1, number) + pb_varint(2, 2) + pb_varint(3, 1)))
    adjustment = (pb_bytes(1, b"agent.queue.adjustment") +
                  pb_bytes(7, pb_bytes(1, pb_fixed64(3, 200) + pb_fixed64(6, 1)) +
                           pb_varint(2, 1) + pb_varint(3, 0)))
    histogram_point = pb_fixed64(3, 200) + pb_fixed64(4, 2) + pb_double(5, 1.5)
    histogram = pb_bytes(1, b"agent.latency") + pb_bytes(9, pb_bytes(1, histogram_point))
    gauge_point = pb_fixed64(3, 200) + pb_double(4, 0.75)
    gauge = pb_bytes(1, b"agent.progress") + pb_bytes(5, pb_bytes(1, gauge_point))
    missing_point = pb_fixed64(3, 250) + pb_double(4, 0.0) + pb_varint(8, 1)
    missing = pb_bytes(1, b"agent.no_reading") + pb_bytes(5, pb_bytes(1, missing_point))
    exp_buckets = pb_varint(1, 2) + pb_varint(2, 2) + pb_varint(2, 1)
    exp_point = (pb_fixed64(3, 200) + pb_fixed64(4, 3) + pb_double(5, 2.5) +
                 pb_varint(6, 2) + pb_bytes(8, exp_buckets))
    exp = pb_bytes(1, b"agent.duration") + pb_bytes(10, pb_bytes(1, exp_point))
    quantile = pb_double(1, 0.5) + pb_double(2, 1.25)
    summary_point = pb_fixed64(3, 200) + pb_fixed64(4, 2) + pb_double(5, 2.5) + pb_bytes(6, quantile)
    summary = pb_bytes(1, b"agent.summary") + pb_bytes(11, pb_bytes(1, summary_point))
    scope = b"".join(pb_bytes(2, item) for item in
                     (metric, adjustment, histogram, gauge, missing, exp, summary))
    return pb_bytes(1, pb_bytes(1, resource) + pb_bytes(2, scope))


class ObservatoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "runs.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_store_connection_context_closes_connection(self):
        with self.store._connect() as db:
            db.execute("SELECT 1").fetchone()
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")

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

    def test_parent_input_and_child_output_usage_are_not_lost_by_deduplication(self):
        wrapper = normalize_native({"trace_id": TID, "span_id": ROOT,
            "name": "invoke_agent", "start_unix_nano": 100, "end_unix_nano": 300,
            "attributes": {"gen_ai.usage.input_tokens": 21}, "events": []})
        model = normalize_native({"trace_id": TID, "span_id": CHILD,
            "parent_span_id": ROOT, "name": "generate_content",
            "start_unix_nano": 150, "end_unix_nano": 250,
            "attributes": {"gen_ai.usage.output_tokens": 8}, "events": []})
        self.store.ingest([wrapper, model])

        run = self.store.traces()[0]
        self.assertEqual((run["input_tokens"], run["output_tokens"]), (21, 8))
        self.assertTrue(run["input_tokens_reported"])
        self.assertTrue(run["output_tokens_reported"])

        reverse_wrapper = normalize_native({"trace_id": "b" * 32, "span_id": ROOT,
            "name": "invoke_agent", "start_unix_nano": 100, "end_unix_nano": 300,
            "attributes": {"gen_ai.usage.output_tokens": 13}, "events": []})
        reverse_model = normalize_native({"trace_id": "b" * 32, "span_id": CHILD,
            "parent_span_id": ROOT, "name": "generate_content",
            "start_unix_nano": 150, "end_unix_nano": 250,
            "attributes": {"gen_ai.usage.input_tokens": 34}, "events": []})
        self.store.ingest([reverse_wrapper, reverse_model])
        reverse_run = next(run for run in self.store.traces() if run["trace_id"] == "b" * 32)
        self.assertEqual((reverse_run["input_tokens"], reverse_run["output_tokens"]), (34, 13))

    def test_token_breakdowns_are_preserved_without_inflating_headline_totals(self):
        attrs = {"gen_ai.usage.input_tokens": 120,
                 "gen_ai.usage.output_tokens": 40,
                 "gen_ai.usage.cache_read.input_tokens": 30,
                 "gen_ai.usage.cache_write.input_tokens": 7,
                 "gen_ai.usage.reasoning_tokens": 11}
        wrapper = normalize_native({"trace_id": TID, "span_id": ROOT,
            "name": "agent.run", "start_unix_nano": 100, "end_unix_nano": 300,
            "attributes": {"session.id": "usage-breakdown-session", **attrs}, "events": []})
        model = normalize_native({"trace_id": TID, "span_id": CHILD,
            "parent_span_id": ROOT, "name": "chat", "start_unix_nano": 150,
            "end_unix_nano": 250, "attributes": attrs, "events": []})
        self.store.ingest([wrapper, model])

        trace = self.store.traces()[0]
        session = self.store.sessions()[0]
        totals = self.store.metrics()
        expected = (30, 7, 11)
        self.assertEqual((trace["cache_read_input_tokens"], trace["cache_write_input_tokens"],
                          trace["reasoning_output_tokens"]), expected)
        self.assertEqual((session["cache_read_input_tokens"], session["cache_write_input_tokens"],
                          session["reasoning_output_tokens"]), expected)
        self.assertEqual((totals["cache_read_input_tokens"], totals["cache_write_input_tokens"],
                          totals["reasoning_output_tokens"]), expected)
        self.assertEqual((trace["input_tokens"], trace["output_tokens"]), (120, 40))

    def test_github_copilot_otel_sessions_preserve_usage_and_surface_lifecycle_events(self):
        root = normalize_native({"trace_id": TID, "span_id": ROOT, "name": "invoke_agent",
            "start_unix_nano": 100, "end_unix_nano": 300, "service": "github-copilot",
            "attributes": {"gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "GitHub Copilot", "gen_ai.conversation.id": "copilot-session",
                "gen_ai.usage.input_tokens": 150, "gen_ai.usage.output_tokens": 40,
                "gen_ai.usage.cache_read.input_tokens": 30,
                "github.copilot.nano_aiu": 2_000_000_000},
            "events": [
                {"name": "github.copilot.hook.start", "time_unix_nano": 110,
                 "attributes": {"github.copilot.hook.type": "preToolUse"}},
                {"name": "github.copilot.hook.end", "time_unix_nano": 120,
                 "attributes": {"github.copilot.hook.type": "preToolUse"}},
                {"name": "github.copilot.skill.invoked", "time_unix_nano": 130,
                 "attributes": {"github.copilot.skill.name": "release-check",
                                "github.copilot.skill.path": "/private/workspace/SKILL.md"}},
                {"name": "github.copilot.session.compaction_start", "time_unix_nano": 140},
                {"name": "github.copilot.session.compaction_complete", "time_unix_nano": 150,
                 "attributes": {"github.copilot.success": True}},
                {"name": "github.copilot.session.truncation", "time_unix_nano": 160,
                 "attributes": {"github.copilot.tokens_removed": 400,
                                "github.copilot.messages_removed": 3}},
                {"name": "github.copilot.session.shutdown", "time_unix_nano": 170,
                 "attributes": {"github.copilot.shutdown_type": "complete"}},
            ]})
        first_chat = normalize_native({"trace_id": TID, "span_id": CHILD, "parent_span_id": ROOT,
            "name": "chat", "start_unix_nano": 120, "end_unix_nano": 200,
            "service": "github-copilot", "attributes": {
                "gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4.1",
                "gen_ai.conversation.id": "copilot-session", "gen_ai.usage.input_tokens": 80,
                "gen_ai.usage.output_tokens": 20, "gen_ai.usage.cache_read.input_tokens": 12}})
        second_chat = normalize_native({"trace_id": TID, "span_id": "3" * 16, "parent_span_id": ROOT,
            "name": "chat", "start_unix_nano": 200, "end_unix_nano": 260,
            "service": "github-copilot", "attributes": {
                "gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4.1",
                "gen_ai.conversation.id": "copilot-session", "gen_ai.usage.input_tokens": 70,
                "gen_ai.usage.output_tokens": 20, "gen_ai.usage.cache_read.input_tokens": 18}})
        tool = normalize_native({"trace_id": TID, "span_id": "4" * 16, "parent_span_id": ROOT,
            "name": "execute_tool", "start_unix_nano": 210, "end_unix_nano": 220,
            "service": "github-copilot", "status": "OK", "attributes": {
                "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "readFile",
                "gen_ai.tool.type": "function", "gen_ai.tool.call.id": "tool-1"}})
        def any_value(value):
            if isinstance(value, bool):
                return {"boolValue": value}
            if isinstance(value, int):
                return {"intValue": str(value)}
            if isinstance(value, float):
                return {"doubleValue": value}
            return {"stringValue": value}

        def otlp_span(span):
            return {"traceId": span["trace_id"], "spanId": span["span_id"],
                "parentSpanId": span["parent_span_id"] or "", "name": span["name"],
                "startTimeUnixNano": str(span["start_unix_nano"]),
                "endTimeUnixNano": str(span["end_unix_nano"]),
                "status": {"code": {"UNSET": 0, "OK": 1, "ERROR": 2}[span["status"]]},
                "attributes": [{"key": key, "value": any_value(value)}
                               for key, value in span["attributes"].items()],
                "events": [{"name": event["name"],
                    "timeUnixNano": str(event["time_unix_nano"]),
                    "attributes": [{"key": key, "value": any_value(value)}
                                   for key, value in event.get("attributes", {}).items()]}
                    for event in span["events"]]}

        payload = {"resourceSpans": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "github-copilot"}}]},
            "scopeSpans": [{"scope": {"name": "github.copilot"}, "spans": [
                otlp_span(span) for span in (root, first_chat, second_chat, tool)]}]}]}
        self.store.ingest(normalize_otlp(payload))

        detail = self.store.trace(TID)
        summary = detail["summary"]
        self.assertEqual((summary["session_id"], summary["input_tokens"], summary["output_tokens"],
                          summary["cache_read_input_tokens"]),
                         ("copilot-session", 150, 40, 30))
        activities = detail["activity"]
        self.assertEqual(len(activities), 8)
        self.assertIn(("hook", "start", "preToolUse", "started"),
                      {(item["domain"], item["action"], item["subject"], item["result"])
                       for item in activities})
        self.assertIn(("skill", "invoke", "release-check", "observed"),
                      {(item["domain"], item["action"], item["subject"], item["result"])
                       for item in activities})
        truncation = next(item for item in activities if item["action"] == "truncate")
        self.assertEqual(truncation["detail"], "400 tokens removed, 3 messages removed")
        self.assertEqual(sum(item["source_id"].startswith(ROOT + ":event:") for item in activities), 7)
        self.assertNotIn("/private/workspace", json.dumps(activities))

    def test_token_breakdown_aliases_are_normalized(self):
        span = normalize_native({"trace_id": "c" * 32, "span_id": ROOT,
            "name": "chat", "start_unix_nano": 100, "end_unix_nano": 200,
            "attributes": {"gen_ai.usage.input_tokens": 40,
                "gen_ai.usage.output_tokens": 12,
                "gen_ai.usage.cached_input_tokens": 8,
                "gen_ai.usage.cache_creation_input_tokens": 2,
                "gen_ai.usage.reasoning.output_tokens": 4}, "events": []})
        self.store.ingest([span])

        trace = next(item for item in self.store.traces() if item["trace_id"] == "c" * 32)
        self.assertEqual((trace["cache_read_input_tokens"], trace["cache_write_input_tokens"],
                          trace["reasoning_output_tokens"]), (8, 2, 4))

    def test_missing_token_usage_is_not_reported_as_a_zero(self):
        span = normalize_native({"trace_id": TID, "span_id": ROOT,
            "name": "agent.run", "start_unix_nano": 100, "end_unix_nano": 200,
            "attributes": {"session.id": "session-without-usage"}, "events": []})
        self.store.ingest([span])
        trace = self.store.traces()[0]
        session = self.store.sessions()[0]
        totals = self.store.metrics()
        self.assertEqual((trace["input_tokens"], trace["output_tokens"]), (0, 0))
        self.assertFalse(trace["input_tokens_reported"])
        self.assertFalse(trace["output_tokens_reported"])
        self.assertFalse(session["input_tokens_reported"])
        self.assertFalse(session["output_tokens_reported"])
        self.assertFalse(session["cache_read_input_tokens_reported"])
        self.assertFalse(session["cache_write_input_tokens_reported"])
        self.assertFalse(session["reasoning_output_tokens_reported"])
        self.assertFalse(totals["input_tokens_reported"])
        self.assertFalse(totals["output_tokens_reported"])
        self.assertFalse(totals["cache_read_input_tokens_reported"])

    def test_reported_zero_token_count_stays_distinct_from_missing_usage(self):
        span = normalize_native({"trace_id": TID, "span_id": ROOT,
            "name": "model call", "start_unix_nano": 100, "end_unix_nano": 200,
            "attributes": {"session.id": "session-with-zero-usage",
                           "gen_ai.usage.input_tokens": 0}, "events": []})
        self.store.ingest([span])
        trace = self.store.traces()[0]
        session = self.store.sessions()[0]
        totals = self.store.metrics()
        self.assertEqual(trace["input_tokens"], 0)
        self.assertTrue(trace["input_tokens_reported"])
        self.assertFalse(trace["output_tokens_reported"])
        self.assertTrue(session["input_tokens_reported"])
        self.assertFalse(session["output_tokens_reported"])
        self.assertTrue(totals["input_tokens_reported"])
        self.assertFalse(totals["output_tokens_reported"])

    def test_existing_v10_summary_cache_is_backfilled_with_token_breakdowns(self):
        span = normalize_native({"trace_id": TID, "span_id": ROOT,
            "name": "agent.run", "start_unix_nano": 100, "end_unix_nano": 200,
            "attributes": {"session.id": "old-cache-session"}, "events": []})
        self.store.ingest([span])
        path = self.store.path
        with self.store._connect() as db:
            row = db.execute("SELECT summary_json FROM trace_summaries WHERE trace_id=?", (TID,)).fetchone()
            old_summary = json.loads(row["summary_json"])
            old_summary.pop("input_tokens_reported", None)
            old_summary.pop("output_tokens_reported", None)
            db.execute("UPDATE trace_summaries SET summary_json=? WHERE trace_id=?",
                       (json.dumps(old_summary), TID))
            db.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('trace_derived_v9','1')")
            db.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('trace_derived_v10','1')")
            db.execute("DELETE FROM schema_meta WHERE key='trace_derived_v11'")
        self.store.close()
        self.store = Store(path)
        upgraded = self.store.traces()[0]
        self.assertFalse(upgraded["input_tokens_reported"])
        self.assertFalse(upgraded["output_tokens_reported"])
        self.assertEqual((upgraded["cache_read_input_tokens"], upgraded["cache_write_input_tokens"],
                          upgraded["reasoning_output_tokens"]), (0, 0, 0))

    def test_nested_model_spans_do_not_double_count_usage_or_cost(self):
        wrapper = normalize_native({"trace_id": TID, "span_id": ROOT, "parent_span_id": None,
            "name": "call_llm", "start_unix_nano": 100, "end_unix_nano": 400,
            "attributes": {"gen_ai.usage.input_tokens": 17,
                           "gen_ai.usage.output_tokens": 9,
                           "llm.cost.total": 0.01}, "events": []})
        provider_call = normalize_native({"trace_id": TID, "span_id": CHILD,
            "parent_span_id": ROOT, "name": "generate_content model-x",
            "start_unix_nano": 150, "end_unix_nano": 250,
            "attributes": {"gen_ai.usage.input_tokens": 17,
                           "gen_ai.usage.output_tokens": 9,
                           "llm.cost.total": 0.01}, "events": []})
        cost_wrapper = normalize_native({"trace_id": TID,
            "span_id": "3333333333333333", "parent_span_id": None,
            "name": "call_llm second", "start_unix_nano": 500,
            "end_unix_nano": 800,
            "attributes": {"llm.cost.total": 0.02}, "events": []})
        second_provider_call = normalize_native({"trace_id": TID,
            "span_id": "4444444444444444", "parent_span_id": "3333333333333333",
            "name": "generate_content model-y", "start_unix_nano": 550,
            "end_unix_nano": 700,
            "attributes": {"gen_ai.usage.input_tokens": 10,
                           "gen_ai.usage.output_tokens": 3}, "events": []})
        self.store.ingest([wrapper, provider_call, cost_wrapper, second_provider_call])
        run = self.store.traces()[0]
        self.assertEqual((run["input_tokens"], run["output_tokens"]), (27, 12))
        self.assertEqual(run["known_cost_usd"], 0.03)
        self.assertTrue(run["cost_complete"])

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

    def test_mcp_is_error_result_becomes_finding_and_failed_call(self):
        transport = normalize_native({"trace_id": TID, "span_id": ROOT,
            "name": "MCP send tools/call lookup_release", "start_unix_nano": 100,
            "end_unix_nano": 200, "status": "UNSET",
            "attributes": {"mcp.method.name": "tools/call",
                           "mcp.tool.name": "lookup_release",
                           "session.id": "session-mcp-1",
                           "agent.task": "Check release status"}, "events": []})
        execution = normalize_native({"trace_id": TID, "span_id": CHILD,
            "name": "execute_tool lookup_release", "start_unix_nano": 100,
            "end_unix_nano": 210, "status": "UNSET",
            "attributes": {"gen_ai.operation.name": "execute_tool",
                           "gen_ai.tool.type": "MCPTool",
                           "gen_ai.tool.name": "lookup_release",
                           "gcp.vertex.agent.tool_response": json.dumps({
                               "isError": True,
                               "content": [{"type": "text", "text":
                                   "release lookup failed; api_key=sk-123456789012345678901234"}],
                           })}, "events": []})
        self.store.ingest([transport, execution])
        finding = next(item for item in self.store.findings()
                       if item["kind"] == "mcp_tool_error")
        self.assertIn("release lookup failed", finding["detail"])
        self.assertIn("[REDACTED", finding["detail"])
        self.assertNotIn("sk-123456789012345678901234", finding["detail"])
        queue_item = next(item for item in self.store.finding_page()["findings"]
                          if item["kind"] == "mcp_tool_error")
        self.assertEqual(queue_item["session_id"], "session-mcp-1")
        self.assertEqual(queue_item["task"], "Check release status")
        call = self.store.mcp_page()["calls"][0]
        self.assertEqual(call["status"], "ERROR")
        self.assertEqual(self.store.mcp_page()["summary"]["errors"], 1)
        self.assertEqual(self.store.traces(status="ERROR")[0]["trace_id"], TID)
        self.assertEqual(self.store.metrics()["errors"], 1)
        activity = self.store.trace(TID)["activity"]
        self.assertTrue(any(item["result"] == "error" for item in activity))
        self.assertEqual(self.store.span_page(TID, span_filter="errors")["total"], 1)

    def test_mcp_inventory_counts_error_shapes_from_shared_result_fields(self):
        result_fields = (
            ("gen_ai.tool.call.result", {"isError": True}),
            ("tool.result", '{"is_error":true,"content":[]}'),
            ("output.value", {"isError": True}),
            ("mcp.tool.is_error", True),
            ("mcp.is_error", True),
        )
        for index, (field, result) in enumerate(result_fields, start=1):
            trace_id = format(index, "032x")
            transport = normalize_native({"trace_id": trace_id, "span_id": ROOT,
                "name": "MCP send tools/call lookup_release", "start_unix_nano": 100,
                "end_unix_nano": 200, "status": "UNSET",
                "attributes": {"mcp.method.name": "tools/call",
                    "mcp.server.name": f"server-{index}", "mcp.tool.name": "lookup_release"},
                "events": []})
            execution_attrs = {"gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.type": "MCPTool", "gen_ai.tool.name": "lookup_release",
                field: result}
            execution = normalize_native({"trace_id": trace_id, "span_id": CHILD,
                "name": "execute_tool lookup_release", "start_unix_nano": 100,
                "end_unix_nano": 210, "status": "UNSET",
                "attributes": execution_attrs, "events": []})
            self.store.ingest([transport, execution])

        page = self.store.mcp_page()
        self.assertEqual(page["summary"]["calls"], len(result_fields))
        self.assertEqual(page["summary"]["errors"], len(result_fields))
        self.assertTrue(all(call["status"] == "ERROR" for call in page["calls"]))

        # Plain text output is common and must not be treated as malformed JSON.
        trace_id = "f" * 32
        plain = normalize_native({"trace_id": trace_id, "span_id": ROOT,
            "name": "MCP send tools/call lookup_release", "start_unix_nano": 100,
            "end_unix_nano": 200, "status": "UNSET",
            "attributes": {"mcp.method.name": "tools/call", "mcp.server.name": "plain",
                "mcp.tool.name": "lookup_release"}, "events": []})
        plain_result = normalize_native({"trace_id": trace_id, "span_id": CHILD,
            "name": "execute_tool lookup_release", "start_unix_nano": 100,
            "end_unix_nano": 210, "status": "UNSET",
            "attributes": {"gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.type": "MCPTool", "gen_ai.tool.name": "lookup_release",
                "tool.result": "ordinary text response"}, "events": []})
        self.store.ingest([plain, plain_result])
        self.assertEqual(self.store.mcp_page()["summary"]["errors"], len(result_fields))

    def test_openllmetry_mcp_client_spans_are_normalized_for_mcp_inventory(self):
        payload = {"resourceSpans": [{"resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "mcp-client"}}]},
            "scopeSpans": [{"scope": {"name": "opentelemetry.instrumentation.mcp.instrumentation",
                                         "version": "0.62.4"},
                "spans": [{"traceId": TID, "spanId": ROOT, "name": "lookup_release.tool",
                    "startTimeUnixNano": "100", "endTimeUnixNano": "200",
                    "status": {"code": 2}, "attributes": [
                        {"key": "traceloop.span.kind", "value": {"stringValue": "tool"}},
                        {"key": "traceloop.entity.name", "value": {"stringValue": "lookup_release"}},
                        {"key": "traceloop.entity.input", "value": {"stringValue": '{"query":"release"}'}},
                        {"key": "traceloop.entity.output", "value": {"stringValue": '{"is_error":true,"content":[]}'}}]}]}]}]}
        self.store.ingest(normalize_otlp(payload))

        call, = self.store.mcp_page()["calls"]
        self.assertEqual((call["method"], call["server"], call["tool"], call["status"]),
                         ("tools/call", "Not reported", "lookup_release", "ERROR"))
        self.assertEqual(call["arguments"], {"query": "release"})
        self.assertEqual(call["output"], {"is_error": True, "content": []})
        self.assertEqual(self.store.mcp_page()["summary"]["errors"], 1)

    def test_mcp_execution_span_ids_are_scoped_to_their_trace(self):
        spans = []
        expected = {}
        for index, trace_id in enumerate((TID, "b" * 32)):
            args = {"query": f"issue-{index}"}
            result = {"matches": [index]}
            spans.append(normalize_native({"trace_id": trace_id, "span_id": ROOT,
                "name": "MCP send tools/call search_issues", "start_unix_nano": 100,
                "end_unix_nano": 200, "attributes": {"mcp.method.name": "tools/call",
                    "mcp.server.name": "github", "mcp.tool.name": "search_issues"}, "events": []}))
            # Span IDs only need to be unique within their trace. Reuse one here
            # to ensure enrichment tracks the trace/span pair.
            spans.append(normalize_native({"trace_id": trace_id, "span_id": CHILD,
                "name": "execute_tool search_issues", "start_unix_nano": 100,
                "end_unix_nano": 210, "attributes": {"gen_ai.tool.type": "MCPTool",
                    "gen_ai.tool.name": "search_issues",
                    "gen_ai.tool.call.arguments": json.dumps(args),
                    "gen_ai.tool.call.result": json.dumps(result)}, "events": []}))
            expected[trace_id] = (args, result)
        self.store.ingest(spans)

        calls = self.store.mcp_page(limit=10)["calls"]
        self.assertEqual(len(calls), 2)
        for call in calls:
            args, result = expected[call["trace_id"]]
            self.assertEqual(call["arguments"], args)
            self.assertEqual(call["output"], result)
            self.assertEqual(call["source"], "agent + MCP spans")

    def test_mcp_execution_type_is_case_insensitive(self):
        transport = normalize_native({"trace_id": TID, "span_id": ROOT,
            "name": "MCP send tools/call lookup_release", "start_unix_nano": 100,
            "end_unix_nano": 200, "attributes": {"mcp.method.name": "tools/call",
                "mcp.tool.name": "lookup_release"}, "events": []})
        execution = normalize_native({"trace_id": TID, "span_id": CHILD,
            "name": "execute_tool lookup_release", "start_unix_nano": 100,
            "end_unix_nano": 210, "attributes": {"gen_ai.tool.type": "McpTool",
                "gen_ai.tool.name": "lookup_release",
                "gen_ai.tool.call.arguments": json.dumps({"project": "demo"}),
                "gen_ai.tool.call.result": json.dumps(
                    {"structuredContent": {"result": "demo: ready"}})}, "events": []})
        self.store.ingest([transport, execution])
        call, = self.store.mcp_page()["calls"]
        self.assertEqual(call["arguments"], {"project": "demo"})
        self.assertEqual(call["output"], {"structuredContent": {"result": "demo: ready"}})

    def test_mcp_inventory_includes_discovery_resource_and_prompt_methods(self):
        examples = [
            (TID, ROOT, {"rpc.system": "mcp", "rpc.method": "tools/list",
                         "server.address": "github"}),
            ("b" * 32, CHILD, {"mcp.server.name": "filesystem",
                               "mcp.method.name": "resources/read"}),
            ("c" * 32, "3" * 16, {"mcp.server.name": "prompts",
                                   "mcp.method.name": "prompts/get"}),
        ]
        spans = [normalize_native({"trace_id": trace_id, "span_id": span_id,
            "name": method, "start_unix_nano": 100, "end_unix_nano": 200,
            "attributes": {**attrs, "session.id": "mcp-methods"}, "events": []})
            for (trace_id, span_id, attrs), method in zip(examples,
                ("tools/list", "resources/read", "prompts/get"))]
        self.store.ingest(spans)

        page = self.store.mcp_page(session_id="mcp-methods")
        self.assertEqual(page["summary"]["calls"], 3)
        self.assertEqual({call["method"] for call in page["calls"]},
                         {"tools/list", "resources/read", "prompts/get"})
        self.assertTrue(all(call["tool"] == "Not reported" for call in page["calls"]))
        self.assertEqual({row["server"] for row in page["summary"]["servers"]},
                         {"github", "filesystem", "prompts"})

    def test_finding_page_counts_matching_occurrences_across_all_sessions(self):
        spans = []
        for index, (trace_id, span_id) in enumerate(((TID, ROOT), ("b" * 32, CHILD))):
            spans.append(normalize_native({"trace_id": trace_id, "span_id": span_id,
                "name": "tool.call", "start_unix_nano": 100 + index,
                "end_unix_nano": 200 + index, "status": "ERROR",
                "attributes": {"tool.name": "lookup", "tool.error_message": "network timeout",
                               "session.id": f"session-{index}"}, "events": []}))
        self.store.ingest(spans)
        finding = next(item for item in self.store.finding_page(limit=1)["findings"]
                       if item["kind"] == "span_error")
        self.assertIn("time_unix_nano", finding)
        self.assertEqual(finding["occurrence_count"], 2)
        self.assertEqual(finding["trace_count"], 2)
        self.assertEqual(finding["session_count"], 2)

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
        self.assertFalse(detail["summary"]["input_tokens_reported"])
        self.assertFalse(detail["summary"]["output_tokens_reported"])
        self.assertEqual(detail["progress"][0]["message"], "Choosing tools")

    def test_existing_database_is_migrated_for_otlp_context(self):
        path = Path(self.temp.name) / "legacy.db"
        db = sqlite3.connect(path)
        try:
            db.execute("""CREATE TABLE spans(trace_id TEXT,span_id TEXT,parent_span_id TEXT,
                name TEXT,start_ns INTEGER,end_ns INTEGER,status TEXT,service TEXT,
                attributes TEXT,events TEXT,PRIMARY KEY(trace_id,span_id))""")
        finally:
            db.close()
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

    def test_gemini_cli_otlp_tool_events_appear_in_activity_and_mcp_views(self):
        logs = {"resourceLogs": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "gemini-cli"}}]},
            "scopeLogs": [{"scope": {"name": "gemini-cli.telemetry"}, "logRecords": [{
                "timeUnixNano": "300", "traceId": TID, "spanId": CHILD,
                "eventName": "gemini_cli.tool_call",
                "attributes": [
                    {"key": "session.id", "value": {"stringValue": "gemini-session"}},
                    {"key": "function_name", "value": {"stringValue": "search_issues"}},
                    {"key": "function_args", "value": {"stringValue": '{"query":"release"}'}},
                    {"key": "tool_type", "value": {"stringValue": "mcp"}},
                    {"key": "mcp_server_name", "value": {"stringValue": "github"}},
                    {"key": "duration_ms", "value": {"intValue": "28"}},
                    {"key": "success", "value": {"boolValue": False}},
                ]}]}]}]}
        normalized = normalize_otlp_logs(logs)
        self.store.ingest_logs(normalized)
        calls = self.store.mcp_page()["calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["server"], calls[0]["tool"], calls[0]["status"]),
                         ("github", "search_issues", "ERROR"))
        self.assertEqual(calls[0]["arguments"], '{"query":"release"}')
        activity = self.store.activity_page()["activity"]
        self.assertEqual(len(activity), 1)
        self.assertEqual((activity[0]["domain"], activity[0]["action"], activity[0]["result"]),
                         ("mcp", "tool_call", "error"))
        self.assertEqual(activity[0]["session_id"], "gemini-session")

    def test_cursor_enterprise_otlp_logs_create_sessions_usage_activity_and_mcp_calls(self):
        def record(stamp, event, event_id, attrs, severity=9, body=None):
            entries = [{"key": key, "value": ({"intValue": value} if isinstance(value, int)
                        else {"stringValue": value})} for key, value in attrs.items()]
            entries.append({"key": "cursor.event.id", "value": {"stringValue": event_id}})
            return {"timeUnixNano": str(stamp), "eventName": event,
                    "severityNumber": severity, "attributes": entries,
                    "body": {"stringValue": body} if body is not None else {}}

        common = {"cursor.conversation.id": "cursor-session-42",
                  "cursor.model.name": "claude-sonnet-4", "vcs.repository.url":
                  "https://github.com/acme/payments.git"}
        records = [
            record(90, "cursor.conversation.user_message", "event-prompt", common,
                   body="Find open payment incidents"),
            record(100, "cursor.api.request", "event-request", {
                **common, "cursor.api.request.input_tokens": 120,
                "cursor.api.request.output_tokens": 35,
                "cursor.api.request.cache_read_tokens": 4,
                "cursor.api.request.cache_creation_tokens": 2}),
            record(200, "cursor.grok_bot.mcp_tool_call", "event-mcp", {
                **common, "cursor.tool.name": "search_issues",
                "cursor.tool.status": "failure", "cursor.mcp.server.name": "github",
                "cursor.grok_bot.mcp.duration_ms": 19,
                "cursor.grok_bot.tool_call.id": "call-001",
                "cursor.grok_bot.event.sequence": 7}, severity=17),
            record(210, "cursor.conversation.tool_io", "event-mcp-args", {
                **common, "cursor.tool.name": "search_issues", "cursor.tool.status": "failure",
                "cursor.mcp.server.name": "github", "cursor.grok_bot.tool_call.id": "call-001",
                "cursor.conversation.tool_io.direction": "arguments",
                "cursor.grok_bot.event.sequence": 8}, body='{"query":"build"}'),
            record(220, "cursor.conversation.tool_io", "event-mcp-result", {
                **common, "cursor.tool.name": "search_issues", "cursor.tool.status": "failure",
                "cursor.mcp.server.name": "github", "cursor.grok_bot.tool_call.id": "call-001",
                "cursor.conversation.tool_io.direction": "result",
                "cursor.grok_bot.event.sequence": 9}, body='{"error":"denied"}'),
            record(300, "cursor.cloud_agent.setup", "event-complete", {
                "cursor.conversation.id": "cursor-cloud-run",
                "cursor.cloud_agent.setup.kind": "completed"}),
            record(400, "cursor.grok_bot.routine_run", "event-routine", {
                "cursor.conversation.id": "cursor-routine-run"}),
            record(500, "cursor.grok_bot.guardrail", "event-wait", {
                "cursor.conversation.id": "cursor-waiting",
                "cursor.grok_bot.guardrail.kind": "tool_escalation",
                "cursor.tool.name": "shell"}),
            record(600, "cursor.grok_bot.routine_run", "event-routine-error", {
                "cursor.conversation.id": "cursor-routine-error",
                "cursor.grok_bot.routine_run.outcome": "error"}),
            record(700, "cursor.grok_bot.routine_run", "event-routine-cancelled", {
                "cursor.conversation.id": "cursor-routine-cancelled",
                "cursor.grok_bot.routine_run.outcome": "cancelled"}),
        ]
        payload = {"resourceLogs": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "cursor"}},
            {"key": "cursor.surface", "value": {"stringValue": "grok_bot"}}]},
            "scopeLogs": [{"scope": {"name": "cursor.telemetry"}, "logRecords": records}]}]}
        logs = normalize_otlp_logs(payload)
        self.store.ingest_logs(logs)
        self.store.ingest_logs(logs[:1])  # stable Cursor event IDs deduplicate retries

        sessions = {row["session_id"]: row for row in self.store.sessions()}
        session = sessions["cursor-session-42"]
        self.assertEqual(session["project"], "payments")
        self.assertEqual(session["latest_task"], "Find open payment incidents")
        self.assertEqual(session["models"], ["claude-sonnet-4"])
        self.assertEqual((session["input_tokens"], session["output_tokens"]), (126, 35))
        self.assertEqual((session["cache_read_input_tokens"], session["cache_write_input_tokens"],
                          session["reasoning_output_tokens"]), (4, 2, 0))
        self.assertTrue(session["cache_read_input_tokens_reported"])
        self.assertTrue(session["cache_write_input_tokens_reported"])
        self.assertTrue(session["input_tokens_reported"])
        self.assertTrue(session["output_tokens_reported"])
        totals = self.store.metrics()
        self.assertEqual((totals["input_tokens"], totals["output_tokens"]), (126, 35))
        self.assertEqual((totals["cache_read_input_tokens"], totals["cache_write_input_tokens"]), (4, 2))
        self.assertTrue(totals["input_tokens_reported"])
        self.assertTrue(totals["output_tokens_reported"])
        self.assertEqual(session["latest_status"], "ERROR")
        calls = self.store.mcp_page(session_id="cursor-session-42")["calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["server"], calls[0]["tool"], calls[0]["status"]),
                         ("github", "search_issues", "ERROR"))
        self.assertEqual(calls[0]["duration_ms"], 19)
        self.assertEqual((calls[0]["arguments"], calls[0]["output"]),
                         ('{"query":"build"}', '{"error":"denied"}'))
        self.assertEqual(calls[0]["metadata"]["cursor.grok_bot.tool_call.id"], "call-001")
        activity = self.store.activity_page(session_id="cursor-session-42")["activity"]
        self.assertEqual(len(activity), 5)
        self.assertEqual(activity[0]["domain"], "mcp")
        self.assertEqual(activity[0]["metadata"]["cursor.event.id"], "event-mcp-result")
        self.assertEqual(activity[0]["metadata"]["cursor.surface"], "grok_bot")
        self.assertEqual(activity[0]["metadata"]["cursor.grok_bot.event.sequence"], 9)
        self.assertEqual(sessions["cursor-cloud-run"]["latest_status"], "RUNNING")
        self.assertEqual(sessions["cursor-routine-run"]["latest_status"], "OK")
        self.assertEqual((sessions["cursor-waiting"]["latest_status"],
                          sessions["cursor-waiting"]["waiting_for"]), ("WAITING", "Approval · shell"))
        self.assertEqual(sessions["cursor-routine-error"]["latest_status"], "ERROR")
        self.assertEqual(sessions["cursor-routine-cancelled"]["latest_status"], "INCOMPLETE")

    def test_log_only_session_usage_aggregates_all_events_not_just_activity_preview(self):
        records = []
        for index in range(60):
            attrs = {
                "cursor.conversation.id": "long-cursor-session",
                "cursor.model.name": "model-x",
                "cursor.api.request.input_tokens": 1,
                "cursor.api.request.output_tokens": 3,
                "cursor.api.request.cache_read_tokens": 2,
                "cursor.api.request.cache_creation_tokens": 1,
                "cursor.api.request.reasoning_tokens": 1,
                "cursor.event.id": f"usage-{index}",
            }
            records.append({"timeUnixNano": str(1000 + index),
                "eventName": "cursor.api.request",
                "attributes": [{"key": key, "value": ({"intValue": value} if isinstance(value, int)
                    else {"stringValue": value})} for key, value in attrs.items()]})
        alias_attrs = {
            "session.id": "framework-log-session",
            "llm.token_count.prompt": 10,
            "llm.token_count.completion": 5,
            "llm.usage.cache_read_input_tokens": 2,
            "llm.usage.cache_creation_input_tokens": 1,
            "llm.usage.reasoning_tokens": 3,
        }
        records.append({"timeUnixNano": "2000", "eventName": "agent.activity",
            "attributes": [{"key": key, "value": ({"intValue": value} if isinstance(value, int)
                else {"stringValue": value})} for key, value in alias_attrs.items()]})
        payload = {"resourceLogs": [{"resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "cursor"}}]},
            "scopeLogs": [{"scope": {"name": "cursor.telemetry"}, "logRecords": records}]}]}
        self.store.ingest_logs(normalize_otlp_logs(payload))

        session, = [item for item in self.store.sessions() if item["session_id"] == "long-cursor-session"]
        alias_session, = [item for item in self.store.sessions() if item["session_id"] == "framework-log-session"]
        totals = self.store.metrics()
        self.assertEqual((session["input_tokens"], session["output_tokens"]), (240, 180))
        self.assertEqual((session["cache_read_input_tokens"], session["cache_write_input_tokens"],
                          session["reasoning_output_tokens"]), (120, 60, 60))
        self.assertEqual((alias_session["input_tokens"], alias_session["output_tokens"],
                          alias_session["cache_read_input_tokens"], alias_session["cache_write_input_tokens"],
                          alias_session["reasoning_output_tokens"]), (10, 5, 2, 1, 3))
        self.assertEqual((totals["input_tokens"], totals["output_tokens"],
                          totals["cache_read_input_tokens"], totals["cache_write_input_tokens"],
                          totals["reasoning_output_tokens"]), (250, 185, 122, 61, 63))

    def test_activity_filter_can_page_only_user_prompts(self):
        logs = {"resourceLogs": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "codex"}}]},
            "scopeLogs": [{"logRecords": [
                {"timeUnixNano": "300", "eventName": "agent.activity", "body": {"stringValue": "Ship the fix"},
                 "attributes": [
                    {"key": "session.id", "value": {"stringValue": "prompt-session"}},
                    {"key": "agent.activity.domain", "value": {"stringValue": "message"}},
                    {"key": "agent.activity.action", "value": {"stringValue": "user"}},
                    {"key": "agent.activity.subject", "value": {"stringValue": "Ship the fix"}},
                    {"key": "agent.activity.result", "value": {"stringValue": "observed"}}]},
                {"timeUnixNano": "301", "eventName": "agent.activity", "body": {"stringValue": "I will check"},
                 "attributes": [
                    {"key": "session.id", "value": {"stringValue": "prompt-session"}},
                    {"key": "agent.activity.domain", "value": {"stringValue": "message"}},
                    {"key": "agent.activity.action", "value": {"stringValue": "assistant"}},
                    {"key": "agent.activity.subject", "value": {"stringValue": "I will check"}},
                    {"key": "agent.activity.result", "value": {"stringValue": "observed"}}]}]}]}]}
        self.store.ingest_logs(normalize_otlp_logs(logs))
        prompts = self.store.activity_page(session_id="prompt-session", domain="message", action="user")["activity"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual((prompts[0]["domain"], prompts[0]["action"], prompts[0]["subject"]),
                         ("message", "user", "Ship the fix"))

    def test_cursor_enterprise_protobuf_log_shape_is_normalized(self):
        def attr(key, value):
            any_value = pb_varint(3, value) if isinstance(value, int) else pb_bytes(1, value.encode())
            return pb_bytes(1, key.encode()) + pb_bytes(2, any_value)

        record = (pb_fixed64(1, 500) + pb_varint(2, 9) + pb_bytes(3, b"INFO") +
                  pb_bytes(5, pb_bytes(1, b"api_request")) +
                  pb_bytes(6, attr("cursor.conversation.id", "cursor-protobuf")) +
                  pb_bytes(6, attr("cursor.event.id", "protobuf-event")) +
                  pb_bytes(6, attr("cursor.api.request.input_tokens", 88)) +
                  pb_bytes(6, attr("cursor.api.request.output_tokens", 21)) +
                  pb_bytes(6, attr("cursor.model.name", "gpt-5")) +
                  pb_bytes(12, b"cursor.api.request"))
        resource = pb_bytes(1, attr("service.name", "cursor"))
        scope = pb_bytes(1, pb_bytes(1, b"cursor.telemetry")) + pb_bytes(2, record)
        payload = pb_bytes(1, pb_bytes(1, resource) + pb_bytes(2, scope))
        self.store.ingest_logs(decode_log_request(payload))

        session, = self.store.sessions()
        self.assertEqual(session["session_id"], "cursor-protobuf")
        self.assertEqual(session["models"], ["gpt-5"])
        self.assertEqual((session["input_tokens"], session["output_tokens"]), (88, 21))

    def test_global_usage_includes_untraced_logs_without_double_counting_traced_sessions(self):
        root = normalize_native({"trace_id": TID, "span_id": ROOT, "name": "agent.run",
            "start_unix_nano": 100, "end_unix_nano": 300,
            "attributes": {"session.id": "traced-usage"}, "events": []})
        model = normalize_native({"trace_id": TID, "span_id": CHILD, "parent_span_id": ROOT,
            "name": "chat", "start_unix_nano": 150, "end_unix_nano": 250,
            "attributes": {"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 30,
                           "gen_ai.usage.output_tokens": 5}, "events": []})
        self.store.ingest([root, model])
        logs = {"resourceLogs": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "cursor"}}]},
            "scopeLogs": [{"logRecords": [
                {"timeUnixNano": "400", "eventName": "agent.activity", "attributes": [
                    {"key": "session.id", "value": {"stringValue": "traced-usage"}},
                    {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "30"}},
                    {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "5"}}]},
                {"timeUnixNano": "500", "eventName": "agent.activity", "attributes": [
                    {"key": "session.id", "value": {"stringValue": "log-only-usage"}},
                    {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "7"}},
                    {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "3"}}]},
            ]}]}]}
        self.store.ingest_logs(normalize_otlp_logs(logs))
        totals = self.store.metrics()
        self.assertEqual((totals["input_tokens"], totals["output_tokens"]), (37, 8))
        self.assertEqual((totals["log_only_input_tokens"], totals["log_only_output_tokens"]), (7, 3))

    def test_usage_series_includes_log_only_usage_and_excludes_trace_duplicates(self):
        now_ns = time.time_ns()
        trace_root = normalize_native({"trace_id": TID, "span_id": ROOT,
            "name": "agent.run", "start_unix_nano": now_ns-1_000_000,
            "end_unix_nano": now_ns, "attributes": {"session.id": "series-traced"}, "events": []})
        model = normalize_native({"trace_id": TID, "span_id": CHILD,
            "parent_span_id": ROOT, "name": "chat", "start_unix_nano": now_ns-500_000,
            "end_unix_nano": now_ns, "attributes": {"gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 20}, "events": []})
        self.store.ingest([trace_root, model])

        def record(stamp, session_id, attrs, severity=9):
            values = {"session.id": session_id, **attrs}
            return {"timeUnixNano": str(stamp), "eventName": "agent.activity",
                "severityNumber": severity,
                "attributes": [{"key": key, "value": ({"intValue": value} if isinstance(value, int)
                    else {"stringValue": value})} for key, value in values.items()]}

        logs = {"resourceLogs": [{"resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "framework-agent"}}]},
            "scopeLogs": [{"logRecords": [
                record(now_ns, "series-log-only", {
                    "llm.token_count.prompt": 10, "llm.token_count.completion": 5,
                    "llm.usage.cache_read_input_tokens": 2,
                    "llm.usage.cache_creation_input_tokens": 1,
                    "llm.usage.reasoning_tokens": 3,
                    "agent.activity.result": "error"}, severity=17),
                record(now_ns, "series-traced", {
                    "gen_ai.usage.input_tokens": 100,
                    "gen_ai.usage.output_tokens": 20}),
            ]}]}]}
        self.store.ingest_logs(normalize_otlp_logs(logs))

        result = self.store.usage_series(days=1, step="hour")
        point = next(item for item in result["points"] if item["input_tokens"])
        self.assertEqual((point["runs"], point["input_tokens"], point["output_tokens"]), (1, 110, 25))
        self.assertEqual((point["cache_read_input_tokens"], point["cache_write_input_tokens"],
                          point["reasoning_output_tokens"]), (2, 1, 3))
        self.assertEqual((point["log_events"], point["log_usage_events"], point["log_errors"]), (1, 1, 1))
        self.assertTrue(point["cache_read_input_tokens_reported"])
        self.assertTrue(point["input_tokens_reported"])

        scoped = self.store.usage_series(days=1, step="hour", service="framework-agent")
        scoped_point = next(item for item in scoped["points"] if item["input_tokens"])
        self.assertEqual((scoped_point["runs"], scoped_point["input_tokens"],
                          scoped_point["output_tokens"]), (0, 10, 5))

        log_session = self.store.usage_series(days=1, step="hour", session_id="series-log-only")
        self.assertEqual(log_session["session_id"], "series-log-only")
        log_point = next(item for item in log_session["points"] if item["input_tokens"])
        self.assertEqual((log_point["runs"], log_point["input_tokens"], log_point["log_events"]),
                         (0, 10, 1))
        traced_session = self.store.usage_series(days=1, step="hour", session_id="series-traced")
        traced_point = next(item for item in traced_session["points"] if item["input_tokens"])
        self.assertEqual((traced_point["runs"], traced_point["input_tokens"],
                          traced_point["output_tokens"], traced_point["log_events"]), (1, 100, 20, 0))

    def test_cursor_mcp_metric_points_are_summarized_without_fabricating_calls(self):
        def attr(key, value):
            value_type = "intValue" if isinstance(value, int) else "stringValue"
            return {"key": key, "value": {value_type: str(value) if isinstance(value, int) else value}}

        metric = {"name": "cursor.tool.calls", "unit": "{call}", "sum": {
            "aggregationTemporality": 1, "isMonotonic": True, "dataPoints": [
                {"startTimeUnixNano": "100", "timeUnixNano": "200", "asInt": "2",
                 "attributes": [attr("cursor.tool.kind", "mcp"), attr("cursor.tool.name", "search_issues"),
                                attr("cursor.mcp.server.name", "github"), attr("cursor.tool.status", "success")]},
                {"startTimeUnixNano": "200", "timeUnixNano": "300", "asInt": "1",
                 "attributes": [attr("cursor.tool.kind", "mcp"), attr("cursor.tool.name", "search_issues"),
                                attr("cursor.mcp.server.name", "github"), attr("cursor.tool.status", "failure")]},
            ]}}
        payload = {"resourceMetrics": [{"resource": {"attributes": [
            attr("service.name", "cursor"), attr("cursor.surface", "cli")]},
            "scopeMetrics": [{"scope": {"name": "cursor.telemetry"}, "metrics": [metric]}]}]}
        self.store.ingest_metrics(normalize_otlp_metrics(payload))

        page = self.store.mcp_page()
        self.assertEqual(page["calls"], [])
        self.assertEqual(page["summary"]["metric_calls"], 3)
        self.assertEqual(page["summary"]["metric_servers"], [
            {"server": "github", "calls": 3, "errors": 1, "tools": 1}])
        points = self.store.metric_points(name="cursor.tool.calls")
        self.assertTrue(all(point["data"]["isMonotonic"] for point in points))
        self.assertEqual(self.store.mcp_page(session_id="cursor-session")["summary"]["metric_calls"], 0)

    def test_cursor_cumulative_mcp_metrics_use_latest_point_per_series_and_reset(self):
        def attr(key, value):
            value_type = "intValue" if isinstance(value, int) else "stringValue"
            return {"key": key, "value": {value_type: str(value) if isinstance(value, int) else value}}

        def point(start, stamp, value, tool, status):
            return {"startTimeUnixNano": str(start), "timeUnixNano": str(stamp), "asInt": str(value),
                    "attributes": [attr("cursor.tool.kind", "mcp"), attr("cursor.tool.name", tool),
                                   attr("cursor.mcp.server.name", "github"), attr("cursor.tool.status", status)]}

        metric = {"name": "cursor.tool.calls", "unit": "{call}", "sum": {
            "aggregationTemporality": 2, "isMonotonic": True, "dataPoints": [
                point(100, 200, 2, "search_issues", "success"),
                point(100, 300, 5, "search_issues", "success"),
                # A new start time marks a cumulative series reset. The latest
                # value replaces the earlier epoch instead of being added to it.
                point(400, 500, 3, "search_issues", "success"),
                point(100, 250, 1, "search_issues", "failure"),
                point(100, 350, 2, "search_issues", "failure"),
                point(100, 600, 4, "list_repositories", "success"),
            ]}}
        payload = {"resourceMetrics": [{"resource": {"attributes": [
            attr("service.name", "cursor")]}, "scopeMetrics": [{"scope": {"name": "cursor.telemetry"},
                "metrics": [metric]}]}]}
        self.store.ingest_metrics(normalize_otlp_metrics(payload))

        summary = self.store.mcp_page()["summary"]
        self.assertEqual(summary["metric_calls"], 9)
        self.assertEqual(summary["metric_servers"], [
            {"server": "github", "calls": 9, "errors": 2, "tools": 2}])

    def test_gemini_cli_agent_lifecycle_logs_report_progress_and_completion(self):
        records = []
        for stamp, name in ((100, "gemini_cli.agent.start"), (300, "gemini_cli.agent.finish")):
            records.append({"timeUnixNano": str(stamp), "traceId": TID,
                "eventName": name,
                "attributes": [
                    {"key": "session.id", "value": {"stringValue": "gemini-session"}},
                    {"key": "agent_name", "value": {"stringValue": "gemini-cli"}},
                    {"key": "duration_ms", "value": {"intValue": "200"}},
                    {"key": "turn_count", "value": {"intValue": "1"}},
                ]})
        payload = {"resourceLogs": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "gemini-cli"}}]},
            "scopeLogs": [{"logRecords": records}]}]}
        self.store.ingest_logs(normalize_otlp_logs(payload))
        trace = self.store.trace(TID)
        self.assertEqual([item["stage"] for item in trace["progress"]], ["Agent started", "Agent finished"])
        self.assertEqual(trace["summary"]["status"], "OK")
        self.assertEqual(trace["session_id"], "gemini-session")

    def test_otel_session_roster_includes_latest_status_project_and_span_totals(self):
        root = normalize_native({"trace_id": TID, "span_id": ROOT,
            "name": "invoke_agent", "start_unix_nano": 100,
            "end_unix_nano": 300, "status": "UNSET",
            "attributes": {"session.id": "gemini-session", "agent.task": "Inspect tests",
                            "agent.waiting_for": "User input"},
            "resource_attributes": {"service.name": "gemini-cli",
                                     "vcs.repository.name": "agent-goechala"}, "events": []})
        model = normalize_native({"trace_id": TID, "span_id": CHILD, "parent_span_id": ROOT,
            "name": "generate_content", "start_unix_nano": 150,
            "end_unix_nano": 250, "status": "OK",
            "attributes": {"gen_ai.operation.name": "chat", "gen_ai.request.model": "gemini-2.5-pro",
                           "gen_ai.usage.input_tokens": 123, "gen_ai.usage.output_tokens": 45},
            "resource_attributes": {"service.name": "gemini-cli",
                                     "vcs.repository.name": "agent-goechala"}, "events": []})
        self.store.ingest([root, model])
        session, = self.store.sessions()
        self.assertEqual(session["session_id"], "gemini-session")
        self.assertEqual(session["latest_status"], "OK")
        self.assertEqual(session["latest_task"], "Inspect tests")
        self.assertEqual(session["project"], "agent-goechala")
        self.assertEqual((session["trace_count"], session["span_count"]), (1, 2))
        self.assertEqual((session["input_tokens"], session["output_tokens"]), (123, 45))
        self.assertEqual(session["models"], ["gemini-2.5-pro"])
        self.assertEqual(session["waiting_for"], "User input")

    def test_session_roster_includes_untraced_runtime_logs(self):
        payload = {"resourceLogs": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "gemini-cli"}},
            {"key": "vcs.repository.name", "value": {"stringValue": "agent-goechala"}}]},
            "scopeLogs": [{"logRecords": [
                {"timeUnixNano": "100", "eventName": "gemini_cli.agent.start",
                 "attributes": [
                     {"key": "session.id", "value": {"stringValue": "log-only-session"}},
                     {"key": "agent.task", "value": {"stringValue": "Inspect the release"}},
                     {"key": "agent_name", "value": {"stringValue": "gemini-cli"}}]},
                {"timeUnixNano": "200", "eventName": "gemini_cli.agent.finish",
                 "attributes": [
                     {"key": "session.id", "value": {"stringValue": "log-only-session"}},
                     {"key": "agent_name", "value": {"stringValue": "gemini-cli"}}]},
                {"timeUnixNano": "300", "eventName": "gemini_cli.api_error",
                 "attributes": [
                     {"key": "session.id", "value": {"stringValue": "log-only-error"}},
                     {"key": "model", "value": {"stringValue": "gemini-2.5-pro"}},
                     {"key": "status_code", "value": {"intValue": "500"}}]},
                {"timeUnixNano": "400", "eventName": "agent.progress",
                 "attributes": [
                     {"key": "session.id", "value": {"stringValue": "log-only-wait"}},
                     {"key": "agent.progress.stage", "value": {"stringValue": "awaiting_approval"}},
                     {"key": "agent.progress.message", "value": {"stringValue": "Review permission"}}]},
            ]}]}]}
        self.store.ingest_logs(normalize_otlp_logs(payload))

        sessions = {item["session_id"]: item for item in self.store.sessions()}
        session = sessions["log-only-session"]
        self.assertEqual(session["session_id"], "log-only-session")
        self.assertEqual(session["services"], ["gemini-cli"])
        self.assertEqual(session["project"], "agent-goechala")
        self.assertEqual(session["latest_task"], "Inspect the release")
        self.assertEqual(session["latest_status"], "OK")
        self.assertEqual((session["trace_count"], session["span_count"]), (0, 0))
        failed = sessions["log-only-error"]
        self.assertEqual(failed["latest_status"], "ERROR")
        self.assertEqual((failed["errors"], failed["issues"]), (1, 1))
        self.assertEqual(failed["models"], ["gemini-2.5-pro"])
        waiting = sessions["log-only-wait"]
        self.assertEqual(waiting["latest_status"], "WAITING")
        self.assertEqual(waiting["waiting_for"], "Approval")
        self.assertEqual(self.store.sessions(limit=1)[0]["session_id"], "log-only-wait")

    def test_log_only_roster_does_not_drop_quiet_sessions_behind_busy_logs(self):
        records = [{
            "time_unix_nano": 1,
            "trace_id": None,
            "span_id": None,
            "service": "quiet-agent",
            "scope_name": "agent.telemetry",
            "severity_number": 0,
            "severity_text": "INFO",
            "event_name": "agent.activity",
            "body": "Earlier session event",
            "attributes": {"session.id": "quiet-session", "agent.activity.action": "run_started"},
            "resource_attributes": {},
        }]
        records.extend({
            "time_unix_nano": stamp,
            "trace_id": None,
            "span_id": None,
            "service": "busy-agent",
            "scope_name": "agent.telemetry",
            "severity_number": 0,
            "severity_text": "INFO",
            "event_name": "agent.activity",
            "body": "Tool activity",
            "attributes": {"session.id": "busy-session", "agent.activity.action": "tool_call"},
            "resource_attributes": {},
        } for stamp in range(2, 6002))
        self.store.ingest_logs(records)

        sessions = {item["session_id"]: item for item in self.store.sessions(limit=2)}
        self.assertEqual(set(sessions), {"quiet-session", "busy-session"})
        self.assertEqual(sessions["quiet-session"]["last_unix_nano"], 1)

    def test_gemini_cli_custom_otel_metrics_are_catalogued_and_typed(self):
        payload = {"resourceMetrics": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "gemini-cli"}}]},
            "scopeMetrics": [{"metrics": [{"name": "gemini_cli.tool.call.count",
                "unit": "{call}", "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                    "dataPoints": [{"timeUnixNano": "300", "asInt": "4", "attributes": [
                        {"key": "function_name", "value": {"stringValue": "search_issues"}},
                        {"key": "tool_type", "value": {"stringValue": "mcp"}}]}]}}]}]}]}
        self.store.ingest_metrics(normalize_otlp_metrics(payload))
        metric = self.store.metric_catalog()[0]
        point = self.store.metric_points()[0]
        self.assertEqual((metric["name"], metric["service"]),
                         ("gemini_cli.tool.call.count", "gemini-cli"))
        self.assertEqual(point["data"]["value"], 4)
        self.assertTrue(point["data"]["isMonotonic"])
        self.assertEqual(point["attributes"], {"function_name": "search_issues", "tool_type": "mcp"})

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
        self.assertEqual(len(points), 8)
        self.assertEqual(next(p for p in points if p["name"] == "agent.tokens")["data"]["value"], 123)
        self.assertTrue(next(p for p in points if p["name"] == "agent.tokens")["data"]["isMonotonic"])
        self.assertFalse(next(p for p in points if p["name"] == "agent.queue.adjustment")["data"]["isMonotonic"])
        missing_point = next(p for p in points if p["name"] == "agent.no_reading")
        self.assertEqual(missing_point["data"]["flags"], 1)
        self.assertEqual(metric_value(missing_point), "No recorded value")
        self.assertEqual(next(p for p in points if p["name"] == "agent.latency")["data"]["count"], 2)
        self.assertEqual(next(p for p in points if p["name"] == "agent.duration")["data"]["positive"]["bucketCounts"], [2, 1])
        self.assertEqual(next(p for p in points if p["name"] == "agent.summary")["data"]["quantileValues"][0]["value"], 1.25)
        self.assertEqual(next(p for p in points if p["name"] == "agent.summary")["data"]["temporality_assumed"], "cumulative")
        self.assertEqual(self.store.metric_points(service="json-metrics")[0]["attributes"]["queue"], "work")
        self.assertEqual(self.store.metrics()["metric_points"], 8)
        self.assertEqual(self.store.metrics()["services"], ["json-metrics", "metric-agent"])
        with self.assertRaises(ValueError):
            normalize_otlp_metrics({"resourceMetrics": [{"scopeMetrics": [
                {"metrics": [{"name": "unknown.metric", "futureKind": {"dataPoints": [{}]}}]}]}]})
        nonfinite = normalize_otlp_metrics({"resourceMetrics": [{"scopeMetrics": [
            {"metrics": [{"name": "nan.metric", "gauge": {"dataPoints": [
                {"timeUnixNano": "100", "asDouble": float("nan")} ]}}]}]}]})
        self.assertEqual(nonfinite[0]["data"]["value"], "nan")

    def test_json_metric_no_recorded_value_flag_is_preserved_and_labeled_missing(self):
        point = normalize_otlp_metrics({"resourceMetrics": [{"scopeMetrics": [{"metrics": [{
            "name": "agent.no_reading", "gauge": {"dataPoints": [{
                "timeUnixNano": "100", "asDouble": 0, "flags": "1"}]}}]}]}]})[0]
        self.assertEqual(point["data"]["flags"], 1)
        self.assertEqual(metric_value(point), "No recorded value")

    def test_latest_metric_points_cover_catalog_beyond_global_recent_point_limit(self):
        resource = {"attributes": [{"key": "service.name", "value": {"stringValue": "busy-agent"}}]}
        metrics = []
        for index in range(205):
            metrics.append({"name": f"agent.metric.{index:03}", "gauge": {"dataPoints": [{
                "timeUnixNano": str(index + 1000), "asInt": index,
                "attributes": [{"key": "worker", "value": {"stringValue": "default"}}],
            }]}})
        metrics[0]["gauge"]["dataPoints"].append({
            "timeUnixNano": "1001", "asInt": 999,
            "attributes": [{"key": "worker", "value": {"stringValue": "priority"}}],
        })
        payload = {"resourceMetrics": [{"resource": resource,
            "scopeMetrics": [{"scope": {"name": "agent"}, "metrics": metrics}]}]}
        self.store.ingest_metrics(normalize_otlp_metrics(payload))

        self.assertEqual(len(self.store.metric_catalog(limit=500)), 205)
        self.assertNotIn("agent.metric.000", {
            point["name"] for point in self.store.metric_points(limit=200)
        })
        latest = self.store.metric_latest_points(limit=500)
        self.assertEqual(len(latest), 205)
        oldest_in_catalog = next(point for point in latest if point["name"] == "agent.metric.000")
        self.assertEqual(oldest_in_catalog["data"]["value"], 999)
        self.assertEqual(oldest_in_catalog["attributes"]["worker"], "priority")

    def test_json_summary_has_implicit_cumulative_interpretation(self):
        point = normalize_otlp_metrics({"resourceMetrics": [{"scopeMetrics": [{"metrics": [{
            "name": "agent.responses", "summary": {
                "aggregationTemporality": 1,
                "dataPoints": [{"timeUnixNano": "100", "count": "3", "sum": 8.5,
                    "quantileValues": [{"quantile": 0.5, "value": 2.5}]}]}}]}]}]})[0]
        self.assertEqual(point["temporality"], 0)
        self.assertEqual(point["data"]["temporality_assumed"], "cumulative")

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
        usage_status, usage = request("/api/usage-series?days=1&step=hour&session_id=session-test")
        self.assertEqual(usage_status, 200)
        self.assertEqual(usage["session_id"], "session-test")
        span = {"trace_id": TID, "span_id": ROOT, "name": "agent.run",
                "start_unix_nano": 10, "end_unix_nano": 20, "attributes": {}, "events": []}
        self.assertEqual(request("/api/spans", {"service": "custom-agent", "spans": [span]})[1]["accepted"], 1)
        request("/api/progress", {"trace_id": TID, "stage": "retrieving", "message": "2 docs", "percent": 50})
        self.assertEqual(request("/api/traces")[1]["traces"][0]["services"], ["custom-agent"])
        self.assertIn('next_cursor', request('/api/activity')[1])
        self.assertIn('next_cursor', request('/api/mcp')[1])
        self.assertEqual(request('/api/activity?cursor=invalid')[0], 400)
        self.assertEqual(request('/api/mcp?cursor=invalid')[0], 400)
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
        self.assertEqual(len(request("/api/measurements")[1]["points"]), 7)
        self.assertEqual(len(request("/api/measurements?latest=1&limit=500")[1]["points"]), 7)
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
