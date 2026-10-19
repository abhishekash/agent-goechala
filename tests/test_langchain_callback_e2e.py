"""Verify LangChain callbacks export runnable, tool, usage, and progress data."""
from __future__ import annotations

import tempfile
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

try:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.outputs import ChatGeneration, LLMResult
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.tools import tool
    from observatory.integrations.langchain import LangChainCallbackHandler

    LANGCHAIN_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the minimal install job
    LANGCHAIN_AVAILABLE = False

from observatory.client import Client
from observatory.core import Store
from observatory.server import handler_for


@unittest.skipUnless(
    LANGCHAIN_AVAILABLE,
    "install the [langchain] extra to run this interoperability test",
)
class LangChainCallbackEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "langchain.db")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler_for(self.store, quiet=True)
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.client = Client(
            endpoint=f"http://127.0.0.1:{self.server.server_port}",
            service="langchain-offline-test",
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def test_runnable_tool_model_usage_progress_and_content_privacy(self):
        @tool
        def lookup_release(project: str) -> str:
            """Return a deterministic release status."""
            return f"{project}: ready"

        chain = (
            ChatPromptTemplate.from_template("Check the release for {project}.")
            | FakeListChatModel(responses=["The release is ready."])
            | StrOutputParser()
        )
        with self.client.trace(
            task="Check project release", session_id="langchain-session-18"
        ) as run:
            callbacks = LangChainCallbackHandler().attach(run)
            answer = chain.invoke(
                {"project": "private-project-731"},
                config={"callbacks": [callbacks]},
            )
            tool_answer = lookup_release.invoke(
                {"project": "demo"}, config={"callbacks": [callbacks]}
            )

            # Build the standard LangChain result object with token usage because
            # FakeListChatModel does not report provider usage metadata.
            model_run_id = uuid.uuid4()
            callbacks.on_chat_model_start(
                {"name": "offline-chat", "kwargs": {"model": "fixture-model"}},
                [],
                run_id=model_run_id,
            )
            callbacks.on_llm_end(
                LLMResult(generations=[[
                    ChatGeneration(message=AIMessage(
                        content="private-model-output-842",
                        usage_metadata={"input_tokens": 17, "output_tokens": 6,
                                        "total_tokens": 23},
                    ))
                ]]),
                run_id=model_run_id,
            )
            callbacks.on_custom_event(
                "agent.progress",
                {"stage": "tool", "message": "Release lookup complete", "percent": 100},
            )

        self.assertEqual(answer, "The release is ready.")
        self.assertEqual(tool_answer, "demo: ready")

        traces = self.store.traces(service="langchain-offline-test")
        self.assertEqual(len(traces), 1, traces)
        detail = self.store.trace(traces[0]["trace_id"])
        self.assertEqual(detail["summary"]["task"], "Check project release")
        self.assertEqual(detail["summary"]["session_id"], "langchain-session-18")

        spans = detail["spans"]
        self.assertTrue(any(span["attributes"].get("langchain.run_type") == "chain"
                            for span in spans), spans)
        self.assertTrue(any(span["attributes"].get("tool.name") == "lookup_release"
                            for span in spans), spans)
        model_span = next(
            span for span in spans
            if span["attributes"].get("gen_ai.request.model") == "fixture-model"
        )
        self.assertEqual(model_span["attributes"].get("gen_ai.usage.input_tokens"), 17)
        self.assertEqual(model_span["attributes"].get("gen_ai.usage.output_tokens"), 6)
        self.assertEqual(detail["progress"][0]["stage"], "tool")
        self.assertEqual(detail["progress"][0]["percent"], 100)

        attributes = repr([span["attributes"] for span in spans])
        self.assertNotIn("private-project-731", attributes)
        self.assertNotIn("private-model-output-842", attributes)


if __name__ == "__main__":
    unittest.main()
