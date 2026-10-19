"""Verify OpenAI Agents SDK tracing reaches the local Observatory receiver."""
from __future__ import annotations

import tempfile
import threading
import unittest
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from pathlib import Path

try:
    from agents import Agent, RunConfig, Runner
    from agents.decorators import tool
    from agents.testing import ScriptedModel, assistant_message, function_call
    from agents.tracing import generation_span, set_trace_processors, trace
    from agents.usage import Usage
    from observatory.integrations.openai_agents import _safe_attributes, create_processor

    OPENAI_AGENTS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the minimal install job
    OPENAI_AGENTS_AVAILABLE = False

from observatory.client import Client
from observatory.core import Store
from observatory.server import handler_for


@unittest.skipUnless(
    OPENAI_AGENTS_AVAILABLE,
    "install the [openai-agents] extra to run this interoperability test",
)
class OpenAIAgentsExportEndToEndTests(unittest.TestCase):
    def test_generation_usage_breakdowns_map_to_shared_semantics(self):
        span = SimpleNamespace(export=lambda: {
            "model": "offline-model",
            "usage": {
                "input_tokens": 80,
                "output_tokens": 25,
                "cached_input_tokens": 12,
                "cache_creation_input_tokens": 4,
                "reasoning_output_tokens": 6,
            },
            "input": [{"role": "user", "content": "must not be captured"}],
        })

        attributes = _safe_attributes(span, "generation")

        self.assertEqual(attributes["gen_ai.usage.input_tokens"], 80)
        self.assertEqual(attributes["gen_ai.usage.output_tokens"], 25)
        self.assertEqual(attributes["gen_ai.usage.cache_read.input_tokens"], 12)
        self.assertEqual(attributes["gen_ai.usage.cache_write.input_tokens"], 4)
        self.assertEqual(attributes["gen_ai.usage.reasoning_tokens"], 6)
        self.assertNotIn("gen_ai.usage.reasoning_output_tokens", attributes)
        self.assertFalse(any("must not be captured" in str(value) for value in attributes.values()))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "openai-agents.db")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler_for(self.store, quiet=True)
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.client = Client(
            endpoint=f"http://127.0.0.1:{self.server.server_port}",
            service="openai-agents-offline-test",
        )
        self.processor = create_processor(
            self.client,
            max_batch_size=128,
            schedule_delay=60,
        )
        # Replace the SDK's default OpenAI exporter so this test is entirely local.
        set_trace_processors([self.processor])

    def tearDown(self):
        self.processor.shutdown(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def test_scripted_agent_tool_loop_exports_safe_trace_and_usage(self):
        @tool
        def lookup_release(project: str) -> str:
            """Return a deterministic release status."""
            return f"{project}: ready"

        model = ScriptedModel(
            [
                [
                    function_call(
                        "lookup_release",
                        {"project": "demo"},
                        call_id="call-local-1",
                    )
                ],
                [assistant_message("The demo release is ready.")],
            ],
            emit_traces=True,
            default_usage=Usage(requests=1, input_tokens=23, output_tokens=5),
        )
        agent = Agent(
            name="release-agent",
            instructions="Check the release with the tool, then report its status.",
            model=model,
            tools=[lookup_release],
        )

        with trace("offline-release-check", group_id="session-openai-42"):
            result = Runner.run_sync(
                agent,
                "Check the private project release marker secret-prompt-731.",
                run_config=RunConfig(tracing_disabled=False),
            )
            # ScriptedModel keeps request usage on the run result rather than its
            # generation span. Add real SDK generation spans with usage to exercise
            # the exporter fields supplied by provider-backed models.
            for input_tokens, output_tokens in ((23, 5), (31, 8)):
                with generation_span(
                    input=[{"role": "user", "content": "private-input-731"}],
                    output=[{"role": "assistant", "content": "private-output-842"}],
                    model="offline-model",
                    usage={
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens,
                    },
                ):
                    pass

        self.assertEqual(result.final_output, "The demo release is ready.")
        model.assert_complete()
        self.processor.force_flush()

        runs = self.store.traces(service="openai-agents-offline-test")
        self.assertEqual(len(runs), 1, runs)
        trace_id = runs[0]["trace_id"]
        detail = self.store.trace(trace_id)
        self.assertEqual(detail["summary"]["session_id"], "session-openai-42")
        self.assertEqual(detail["summary"]["services"], ["openai-agents-offline-test"])

        spans = detail["spans"]
        names = [span["name"] for span in spans]
        self.assertIn("openai_agents.agent", names)
        self.assertIn("openai_agents.generation", names)
        self.assertIn("openai_agents.function", names)

        generations = [span for span in spans if span["name"] == "openai_agents.generation"]
        self.assertGreaterEqual(len(generations), 2)
        self.assertEqual(
            sum(span["attributes"].get("gen_ai.usage.input_tokens", 0) for span in generations),
            54,
            generations,
        )
        self.assertEqual(
            sum(span["attributes"].get("gen_ai.usage.output_tokens", 0) for span in generations),
            13,
        )
        self.assertTrue(
            any(span["attributes"].get("tool.name") == "lookup_release" for span in spans),
            spans,
        )
        self.assertTrue(
            all("session.id" in span["attributes"] for span in spans),
            spans,
        )

        serialized_attributes = repr([span["attributes"] for span in spans])
        self.assertNotIn("secret-prompt-731", serialized_attributes)
        self.assertNotIn("The demo release is ready.", serialized_attributes)
        self.assertNotIn("private-input-731", serialized_attributes)
        self.assertNotIn("private-output-842", serialized_attributes)


if __name__ == "__main__":
    unittest.main()
