"""Exercise the receiver with real OpenTelemetry SDK OTLP exporters."""
from __future__ import annotations

import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

try:
    from opentelemetry import trace
    from opentelemetry._logs import LogRecord
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import Status, StatusCode

    OTLP_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in the minimal install job
    OTLP_SDK_AVAILABLE = False

from observatory.core import Store
from observatory.server import handler_for


@unittest.skipUnless(OTLP_SDK_AVAILABLE, "install the [auto] extra for OTLP SDK interoperability tests")
class OTLPExportEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "otel.db")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.store, quiet=True))
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"
        self.resource = Resource.create({
            "service.name": "otel-interop-agent",
            "host.name": "test-host",
            "deployment.environment.name": "test",
        })
        self.providers = []

    def tearDown(self):
        for provider in reversed(self.providers):
            provider.shutdown()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def test_otlp_exports_agent_trace_progress_mcp_and_metrics(self):
        tracer_provider = TracerProvider(resource=self.resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=f"{self.endpoint}/v1/traces", timeout=5)))
        self.providers.append(tracer_provider)

        logger_provider = LoggerProvider(resource=self.resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(
            endpoint=f"{self.endpoint}/v1/logs", timeout=5)))
        self.providers.append(logger_provider)

        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter(
            endpoint=f"{self.endpoint}/v1/metrics", timeout=5),
            export_interval_millis=60_000, export_timeout_millis=5_000)
        meter_provider = MeterProvider(resource=self.resource, metric_readers=[metric_reader])
        self.providers.append(meter_provider)

        tracer = tracer_provider.get_tracer("agent.instrumentation", "1.0")
        logger = logger_provider.get_logger("agent.progress", "1.0")
        meter = meter_provider.get_meter("agent.runtime", "1.0")
        calls = meter.create_counter("agent.mcp.calls", unit="{call}")

        with tracer.start_as_current_span("invoke_agent") as root:
            root.set_attribute("gen_ai.operation.name", "invoke_agent")
            root.set_attribute("gen_ai.agent.name", "research-agent")
            root.set_attribute("gen_ai.conversation.id", "session-otel-42")
            root.set_attribute("agent.task", "Look up the release status")
            trace_id = f"{root.get_span_context().trace_id:032x}"

            with tracer.start_as_current_span("chat gpt-test") as model:
                model.set_attribute("gen_ai.operation.name", "chat")
                model.set_attribute("gen_ai.request.model", "gpt-test")
                model.set_attribute("gen_ai.usage.input_tokens", 37)
                model.set_attribute("gen_ai.usage.output_tokens", 12)

            with tracer.start_as_current_span("execute_tool github.search_issues") as tool:
                tool.set_attribute("gen_ai.operation.name", "execute_tool")
                tool.set_attribute("rpc.system", "mcp")
                tool.set_attribute("rpc.method", "tools/call")
                tool.set_attribute("mcp.server.name", "github")
                tool.set_attribute("mcp.tool.name", "search_issues")
                tool.set_attribute("gen_ai.tool.call.id", "call-otel-1")
                tool.set_attribute("tool.input", '{"query":"release status"}')
                tool.set_attribute("tool.output", '{"state":"open"}')
                calls.add(1, {"mcp.server.name": "github", "mcp.tool.name": "search_issues"})
                tool.set_status(Status(StatusCode.OK))

            logger.emit(LogRecord(
                context=trace.set_span_in_context(root),
                body="Waiting for GitHub search result",
                attributes={"event.name": "agent.progress",
                            "agent.progress.stage": "tool",
                            "agent.progress.message": "Waiting for GitHub search result",
                            "agent.progress.percent": 65,
                            "session.id": "session-otel-42"},
            ))

        for provider in (tracer_provider, logger_provider, meter_provider):
            self.assertTrue(provider.force_flush(timeout_millis=5_000))

        trace_detail = self.store.trace(trace_id)
        self.assertEqual(trace_detail["summary"]["task"], "Look up the release status")
        self.assertEqual(trace_detail["summary"]["session_id"], "session-otel-42")
        model_span = next(span for span in trace_detail["spans"]
                          if span["attributes"].get("gen_ai.request.model") == "gpt-test")
        self.assertEqual((model_span["attributes"]["gen_ai.usage.input_tokens"],
                          model_span["attributes"]["gen_ai.usage.output_tokens"]), (37, 12))

        mcp_page = self.store.mcp_page()
        self.assertEqual(mcp_page["summary"]["servers"][0]["server"], "github")
        self.assertEqual(mcp_page["calls"][0]["tool"], "search_issues")
        self.assertEqual(mcp_page["calls"][0]["trace_id"], trace_id)

        progress = trace_detail["progress"]
        self.assertEqual(progress[0]["stage"], "tool")
        self.assertEqual(progress[0]["percent"], 65)
        points = self.store.metric_points(name="agent.mcp.calls")
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["data"]["value"], 1)
        self.assertEqual(points[0]["attributes"]["mcp.server.name"], "github")

    def test_openllmetry_mcp_span_is_normalized_through_real_otlp_exporter(self):
        provider = TracerProvider(resource=self.resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=f"{self.endpoint}/v1/traces", timeout=5)))
        self.providers.append(provider)
        tracer = provider.get_tracer(
            "opentelemetry.instrumentation.mcp.instrumentation", "0.62.4")

        with tracer.start_as_current_span("lookup_release.tool") as span:
            span.set_attribute("traceloop.span.kind", "tool")
            span.set_attribute("traceloop.entity.name", "lookup_release")
            span.set_attribute("traceloop.entity.input", '{"query":"release"}')
            span.set_attribute("traceloop.entity.output", '{"is_error":true,"content":[]}')
            span.set_status(Status(StatusCode.ERROR))

        self.assertTrue(provider.force_flush(timeout_millis=5_000))
        call, = self.store.mcp_page()["calls"]
        self.assertEqual((call["method"], call["server"], call["tool"], call["status"]),
                         ("tools/call", "Not reported", "lookup_release", "ERROR"))
        self.assertEqual(call["arguments"], {"query": "release"})
        self.assertEqual(call["output"], {"is_error": True, "content": []})

    def test_gemini_cli_otlp_session_and_mcp_activity_through_http_receiver(self):
        resource = Resource.create({
            "service.name": "gemini-cli",
            "telemetry.sdk.name": "gemini-cli",
        })
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=f"{self.endpoint}/v1/traces", timeout=5)))
        self.providers.append(tracer_provider)

        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(
            endpoint=f"{self.endpoint}/v1/logs", timeout=5)))
        self.providers.append(logger_provider)

        tracer = tracer_provider.get_tracer("gemini-cli.telemetry", "1.0")
        logger = logger_provider.get_logger("gemini-cli.telemetry", "1.0")
        session_id = "gemini-http-session-7"
        with tracer.start_as_current_span("agent.run") as root:
            root.set_attribute("session.id", session_id)
            root.set_attribute("agent.task", "Find the latest package release")
            root.set_attribute("gen_ai.agent.name", "gemini-cli")
            trace_id = f"{root.get_span_context().trace_id:032x}"

            with tracer.start_as_current_span("chat gemini-2.5-pro") as model:
                model.set_attribute("gen_ai.operation.name", "chat")
                model.set_attribute("gen_ai.request.model", "gemini-2.5-pro")
                model.set_attribute("gen_ai.usage.input_tokens", 82)
                model.set_attribute("gen_ai.usage.output_tokens", 19)

            with tracer.start_as_current_span("tools/call packages.latest_release"):
                logger.emit(LogRecord(
                    context=trace.set_span_in_context(trace.get_current_span()),
                    body="MCP tool call completed",
                    event_name="gemini_cli.tool_call",
                    attributes={
                        "session.id": session_id,
                        "function_name": "latest_release",
                        "function_args": '{"package":"agent-goechala"}',
                        "tool_type": "mcp",
                        "mcp_server_name": "packages",
                        "success": True,
                        "duration_ms": 24,
                    },
                ))

            logger.emit(LogRecord(
                context=trace.set_span_in_context(root),
                body="Agent run finished",
                event_name="gemini_cli.agent.finish",
                attributes={"session.id": session_id, "agent_name": "gemini-cli"},
            ))

        for provider in (tracer_provider, logger_provider):
            self.assertTrue(provider.force_flush(timeout_millis=5_000))

        sessions = self.store.sessions()
        session = next(row for row in sessions if row["session_id"] == session_id)
        self.assertEqual(session["latest_task"], "Find the latest package release")
        self.assertEqual(session["models"], ["gemini-2.5-pro"])
        self.assertEqual(session["input_tokens"], 82)
        self.assertEqual(session["output_tokens"], 19)

        trace_detail = self.store.trace(trace_id)
        self.assertEqual(trace_detail["summary"]["session_id"], session_id)
        mcp_call = next(call for call in self.store.mcp_calls(session_id=session_id)
                        if call["server"] == "packages")
        self.assertEqual(mcp_call["tool"], "latest_release")
        self.assertEqual(mcp_call["trace_id"], trace_id)

        activity = self.store.activity_page(session_id=session_id)["activity"]
        mcp_activity = next(item for item in activity if item["domain"] == "mcp")
        self.assertEqual(mcp_activity["action"], "tool_call")
        self.assertEqual(mcp_activity["result"], "success")
        self.assertEqual(mcp_activity["mcp_server"], "packages")


if __name__ == "__main__":
    unittest.main()
