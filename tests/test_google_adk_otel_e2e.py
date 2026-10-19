"""Verify Google ADK traces, tool spans, sessions, and usage over OTLP."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

try:
    from google.adk.agents import LlmAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import InMemoryRunner
    from google.adk.telemetry.setup import maybe_set_otel_providers
    from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
    from google.genai import types
    from mcp import StdioServerParameters
    from opentelemetry import trace

    GOOGLE_ADK_OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - minimal-install CI job
    GOOGLE_ADK_OTEL_AVAILABLE = False

from observatory.core import Store
from observatory.server import handler_for


@unittest.skipUnless(
    GOOGLE_ADK_OTEL_AVAILABLE,
    "install Google ADK and the OTLP exporter to run this interoperability test",
)
class GoogleAdkOtelEndToEndTests(unittest.TestCase):
    SERVICE = "google-adk-offline-test"
    ENV_KEYS = (
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_SERVICE_NAME",
        "OTEL_RESOURCE_ATTRIBUTES",
    )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "google-adk.db")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler_for(self.store, quiet=True)
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.previous_environment = {
            key: os.environ.get(key) for key in self.ENV_KEYS
        }
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = (
            f"http://127.0.0.1:{self.server.server_port}/v1/traces"
        )
        os.environ["OTEL_SERVICE_NAME"] = self.SERVICE
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "deployment.environment.name=test"
        maybe_set_otel_providers()
        self.provider = trace.get_tracer_provider()

    def tearDown(self):
        self.provider.shutdown()
        for key, value in self.previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def test_offline_agent_mcp_tool_and_duplicate_usage_spans(self):
        class FakeModel(BaseLlm):
            model: str = "offline-adk"
            calls: int = 0

            @classmethod
            def supported_models(cls):
                return ["offline-adk"]

            async def generate_content_async(
                self, llm_request: LlmRequest, stream: bool = False
            ):
                self.calls += 1
                if self.calls == 1:
                    part = types.Part(
                        function_call=types.FunctionCall(
                            id="call-1",
                            name="lookup_release",
                            args={"project": "demo"},
                        )
                    )
                else:
                    part = types.Part(text="Release is ready.")
                yield LlmResponse(
                    content=types.Content(role="model", parts=[part]),
                    usageMetadata=types.GenerateContentResponseUsageMetadata(
                        promptTokenCount=17,
                        candidatesTokenCount=9,
                        totalTokenCount=26,
                    ),
                )

        model = FakeModel()
        mcp_server = Path(__file__).parent / "fixtures" / "mcp_server.py"
        toolset = McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=sys.executable,
                    args=[str(mcp_server)],
                ),
                timeout=10,
            )
        )
        agent = LlmAgent(
            name="offline_adk_agent",
            model=model,
            instruction="Use lookup_release, then summarize its result.",
            tools=[toolset],
        )
        runner = InMemoryRunner(agent=agent, app_name="adk-offline-app")

        async def run_agent():
            session = await runner.session_service.create_session(
                app_name="adk-offline-app",
                user_id="test-user",
                session_id="adk-session-17",
            )
            return [
                event async for event in runner.run_async(
                    user_id="test-user",
                    session_id=session.id,
                    new_message=types.Content(
                        role="user", parts=[types.Part(text="Check the demo release")]
                    ),
                )
            ]

        events = asyncio.run(run_agent())
        self.assertEqual(model.calls, 2)
        self.assertTrue(events)
        self.assertTrue(self.provider.force_flush(timeout_millis=5_000))

        runs = self.store.traces(service=self.SERVICE)
        self.assertEqual(len(runs), 1, runs)
        run = runs[0]
        self.assertEqual((run["input_tokens"], run["output_tokens"]), (34, 18))
        self.assertEqual(run["session_id"], "adk-session-17")

        detail = self.store.trace(run["trace_id"])
        spans = detail["spans"]
        operations = [span["attributes"].get("gen_ai.operation.name") for span in spans]
        self.assertIn("invoke_agent", operations)
        self.assertIn("generate_content", operations)
        tool_spans = [span for span in spans if span["name"] == "execute_tool lookup_release"]
        self.assertEqual(len(tool_spans), 1)
        self.assertEqual(tool_spans[0]["attributes"].get("gen_ai.tool.name"), "lookup_release")
        mcp_page = self.store.mcp_page()
        self.assertEqual(mcp_page["summary"]["calls"], 1, {
            "mcp": mcp_page,
            "spans": [(span["name"], span["attributes"]) for span in spans],
        })
        self.assertEqual(mcp_page["calls"][0]["tool"], "lookup_release")
        self.assertEqual(mcp_page["calls"][0]["method"], "tools/call")
        # ADK's current MCP spans identify the tool but omit the configured
        # server name; keep this explicit instead of inventing an identity.
        self.assertEqual(mcp_page["calls"][0]["server"], "Not reported")
        self.assertEqual(mcp_page["summary"]["unknown_server_calls"], 1)
        self.assertEqual(mcp_page["summary"]["unknown_server_tools"], 1)
        self.assertEqual(mcp_page["summary"]["unknown_server_errors"], 0)
        self.assertEqual(mcp_page["calls"][0]["trace_id"], run["trace_id"])
        self.assertEqual(mcp_page["calls"][0]["session_id"], "adk-session-17")
        self.assertEqual(mcp_page["calls"][0]["arguments"], {"project": "demo"})
        self.assertEqual(
            mcp_page["calls"][0]["output"]["structuredContent"],
            {"result": "demo: ready"},
        )
        scoped_mcp_page = self.store.mcp_page(session_id="adk-session-17")
        self.assertEqual(scoped_mcp_page["summary"]["calls"], 1)
        self.assertEqual(scoped_mcp_page["calls"][0]["trace_id"], run["trace_id"])
        model_spans = [
            span for span in spans
            if span["attributes"].get("gen_ai.operation.name") == "generate_content"
        ]
        self.assertEqual(len(model_spans), 2)
        self.assertEqual(
            sum(span["attributes"].get("gen_ai.usage.input_tokens", 0)
                for span in model_spans),
            34,
        )


if __name__ == "__main__":
    unittest.main()
