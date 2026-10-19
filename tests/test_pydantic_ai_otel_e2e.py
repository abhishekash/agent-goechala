"""Verify Pydantic AI's native instrumentation reaches the OTLP receiver."""
from __future__ import annotations

import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from pydantic_ai import Agent
    from pydantic_ai.capabilities import Instrumentation
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import RequestUsage

    PYDANTIC_AI_OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - minimal-install CI job
    PYDANTIC_AI_OTEL_AVAILABLE = False

from observatory.core import Store
from observatory.server import handler_for


@unittest.skipUnless(
    PYDANTIC_AI_OTEL_AVAILABLE,
    "install Pydantic AI and the OTLP exporter to run this interoperability test",
)
class PydanticAIOtelEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "pydantic-ai.db")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler_for(self.store, quiet=True)
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()

    def tearDown(self):
        if hasattr(self, "provider"):
            self.provider.shutdown()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def test_fake_model_agent_and_tool_spans_are_ingested(self):
        endpoint = f"http://127.0.0.1:{self.server.server_port}/v1/traces"
        self.provider = TracerProvider(
            resource=Resource.create({
                "service.name": "pydantic-ai-offline-test",
                "agent.session_id": "pydantic-session-17",
            })
        )
        self.provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=5))
        )
        trace.set_tracer_provider(self.provider)

        model_calls = 0

        def fake_model(messages, info):
            nonlocal model_calls
            model_calls += 1
            if model_calls == 1:
                return ModelResponse(
                    parts=[ToolCallPart("lookup_release", {"project": "demo"}, tool_call_id="call-1")],
                    usage=RequestUsage(input_tokens=23, output_tokens=5),
                )
            return ModelResponse(
                parts=[TextPart("Release is ready.")],
                usage=RequestUsage(input_tokens=31, output_tokens=8),
            )

        def lookup_release(project: str) -> str:
            return f"{project}: ready"

        agent = Agent(
            FunctionModel(fake_model, model_name="offline-model"),
            name="release-agent",
            instructions="Use the tool, then summarize its result.",
            tools=[lookup_release],
            capabilities=[Instrumentation()],
        )
        result = agent.run_sync("Check the demo release")
        self.assertEqual(result.output, "Release is ready.")
        self.assertEqual(model_calls, 2)
        self.assertTrue(self.provider.force_flush(timeout_millis=5_000))

        runs = self.store.traces(service="pydantic-ai-offline-test")
        self.assertEqual(len(runs), 1, runs)
        trace_id = runs[0]["trace_id"]
        detail = self.store.trace(trace_id)
        self.assertIsNotNone(detail["summary"])
        self.assertEqual(detail["summary"]["services"], ["pydantic-ai-offline-test"])
        self.assertTrue(detail["summary"]["session_id"])
        session = next(
            item for item in self.store.sessions()
            if item["session_id"] == detail["summary"]["session_id"]
        )
        self.assertEqual(session["services"], ["pydantic-ai-offline-test"])
        spans = detail["spans"]
        span_names = [span["name"] for span in spans]
        operations = [span["attributes"].get("gen_ai.operation.name") for span in spans]
        self.assertIn("invoke_agent", operations, (span_names, operations))
        self.assertIn("chat", operations, (span_names, operations))
        self.assertIn("execute_tool", operations, (span_names, operations))

        model_spans = [
            span for span in spans
            if span["attributes"].get("gen_ai.request.model") == "offline-model"
        ]
        self.assertGreaterEqual(len(model_spans), 2)
        self.assertEqual(
            sum(int(span["attributes"].get("gen_ai.usage.input_tokens", 0))
                for span in model_spans),
            54,
        )
        self.assertEqual(
            sum(int(span["attributes"].get("gen_ai.usage.output_tokens", 0))
                for span in model_spans),
            13,
        )
        tool_spans = [
            span for span in spans
            if span["attributes"].get("gen_ai.operation.name") == "execute_tool"
        ]
        self.assertEqual(len(tool_spans), 1)
        self.assertEqual(tool_spans[0]["attributes"].get("gen_ai.tool.name"), "lookup_release")


if __name__ == "__main__":
    unittest.main()
