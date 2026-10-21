"""Exercise automatic SDK instrumentation through the real ``observatory run`` CLI."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

try:
    import openai  # noqa: F401
    import anthropic  # noqa: F401
    import openinference.instrumentation  # noqa: F401
    import openinference.instrumentation.openai  # noqa: F401
    import openinference.instrumentation.anthropic  # noqa: F401
    import openinference.instrumentation.langchain  # noqa: F401
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: F401
    from opentelemetry.sdk.trace import TracerProvider  # noqa: F401

    AUTO_INSTRUMENTATION_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the minimal install job
    AUTO_INSTRUMENTATION_AVAILABLE = False

from observatory.core import Store
from observatory.server import handler_for


@unittest.skipUnless(
    AUTO_INSTRUMENTATION_AVAILABLE,
    "install the [auto] extra plus OpenAI, Anthropic, and LangChain SDKs",
)
class AutoLauncherEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "automatic-capture.db")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler_for(self.store, quiet=True)
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def test_cli_captures_mocked_sdk_calls_progress_errors_and_stop(self):
        project = Path(__file__).resolve().parents[1]
        script = project / "examples" / "automatic_capture_demo.py"
        service = "automatic-capture-e2e"
        endpoint = f"http://127.0.0.1:{self.server.server_port}"
        environment = os.environ.copy()
        environment.pop("OBSERVATORY_TOKEN", None)
        environment.pop("OBSERVATORY_ENDPOINT", None)
        environment["OPENAI_API_KEY"] = "local-replay-only"

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "observatory",
                "run",
                "--endpoint",
                endpoint,
                "--service",
                service,
                str(script),
            ],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Finished. Run agenturm", result.stdout)
        self.assertIn("listening to automatic-capture-e2e", result.stderr)

        traces = self.store.traces(service=service)
        self.assertGreaterEqual(len(traces), 3, traces)
        details = [self.store.trace(trace["trace_id"]) for trace in traces]
        spans = [span for detail in details for span in detail["spans"]]
        attributes = [span["attributes"] for span in spans]
        model_names = {
            attrs.get("gen_ai.request.model")
            or attrs.get("llm.model_name")
            or attrs.get("llm.request.model_name")
            or attrs.get("llm.response.model_name")
            for attrs in attributes
        }
        model_names.discard(None)
        self.assertTrue(
            {"local-openai-chat", "local-openai-stream", "local-anthropic-chat"}
            <= model_names,
            model_names,
        )
        self.assertGreater(
            sum(int(attrs.get("gen_ai.usage.input_tokens", 0)
                    or attrs.get("llm.token_count.prompt", 0)
                    or attrs.get("llm.usage.input_tokens", 0))
                for attrs in attributes),
            0,
            attributes,
        )
        self.assertGreater(
            sum(int(attrs.get("gen_ai.usage.output_tokens", 0)
                    or attrs.get("llm.token_count.completion", 0)
                    or attrs.get("llm.usage.output_tokens", 0))
                for attrs in attributes),
            0,
            attributes,
        )
        self.assertTrue(any(span["status"] == "ERROR" for span in spans), spans)
        progress = [event for detail in details for event in detail["progress"]]
        self.assertTrue(
            any(event["stage"] == "Model call started" for event in progress),
            progress,
        )
        self.assertTrue(
            any(connection["service"] == service and connection["state"] == "stopped"
                for connection in self.store.connections()),
            self.store.connections(),
        )
        serialized_attributes = repr(attributes)
        self.assertNotIn("local-only-sensitive-demo-message", serialized_attributes)
        self.assertNotIn("This is a local OpenAI replay.", serialized_attributes)


if __name__ == "__main__":
    unittest.main()
