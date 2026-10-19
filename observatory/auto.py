"""Launch a Python agent with model and framework instrumentation enabled."""
from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
import platform
import queue
import runpy
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .client import Client


class SetupError(Exception):
    """A problem the user can fix before their agent starts."""


INTEGRATIONS = (
    ("openai", "openai", "OpenAIInstrumentor"),
    ("anthropic", "anthropic", "AnthropicInstrumentor"),
    ("langchain_core", "langchain", "LangChainInstrumentor"),
)


def _receiver_endpoint(endpoint):
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise SetupError("Use the receiver's HTTP address for --endpoint, such as http://127.0.0.1:4318.")
    return endpoint.rstrip("/")


def _check_receiver(endpoint, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(endpoint + "/api/health", headers=headers),
                                    timeout=3) as response:
            if response.status != 200:
                raise SetupError("The Goechala receiver is not ready.")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise SetupError("This receiver needs a token. Set OBSERVATORY_TOKEN in your agent's terminal.") from exc
        raise SetupError(f"The receiver returned HTTP {exc.code}. Check --endpoint.") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise SetupError(f"Cannot reach Goechala at {endpoint}. Start the receiver or terminal cockpit first, then retry.") from exc


class _Connection:
    def __init__(self, endpoint, service, integrations, capture_content, token, source_id, events):
        self.client = Client(endpoint=endpoint, service=service, token=token, timeout=2)
        self.payload = {"source_id": source_id, "service": service,
                        "runtime": f"Python {platform.python_version()}",
                        "integrations": integrations, "capture_content": capture_content,
                        "process_id": os.getpid(), "state": "running"}
        self.stop = threading.Event()
        self.events = events
        self.thread = threading.Thread(target=self._heartbeat, name="observatory-connection", daemon=True)

    def start(self):
        if not self.client._post("/api/connections", self.payload):
            raise SetupError("Could not register this agent with Goechala. Check the receiver address and token.")
        self.thread.start()

    def _heartbeat(self):
        next_heartbeat = time.monotonic() + 10
        while not self.stop.is_set() or not self.events.empty():
            try:
                event = self.events.get(timeout=0.5)
                self.client._post("/api/progress", event)
            except queue.Empty:
                pass
            if time.monotonic() >= next_heartbeat:
                self.client._post("/api/connections", self.payload)
                next_heartbeat = time.monotonic() + 10

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)
        self.client._post("/api/connections", {**self.payload, "state": "stopped"})


def run_agent(target, arguments=(), *, endpoint="http://127.0.0.1:4318", service="my-agent",
              module=False, capture_content=False):
    """Instrument installed SDKs, then run a script or module in this interpreter."""
    endpoint = _receiver_endpoint(endpoint)
    service = service.strip()
    if not service or len(service) > 128:
        raise SetupError("Choose a service name between 1 and 128 characters.")
    path = Path(target).resolve() if not module else None
    if path is not None and not path.is_file():
        raise SetupError(f"Cannot find {target}. Run this command from your agent's folder or supply its full path.")
    try:
        from openinference.instrumentation import TraceConfig
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import SpanLimits, TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace import SpanProcessor
        from packaging.requirements import Requirement
    except ImportError as exc:
        raise SetupError('Automatic capture is not installed in this Python environment. Install this checkout with: python -m pip install ".[auto]"') from exc

    token = os.environ.get("OBSERVATORY_TOKEN")
    _check_receiver(endpoint, token)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    source_id = secrets.token_hex(16)
    events = queue.Queue(maxsize=256)
    provider = TracerProvider(resource=Resource.create({"service.name": service,
        "host.name": socket.gethostname(), "host.arch": platform.machine(),
        "os.type": platform.system().lower(), "observatory.capture": "automatic",
        "observatory.source_id": source_id}),
        span_limits=SpanLimits(max_attribute_length=4000))
    provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=endpoint + "/v1/traces", headers=headers, timeout=3),
        schedule_delay_millis=500, max_export_batch_size=64))

    class FrameworkLinks(SpanProcessor):
        def on_start(self, span, parent_context=None):
            # LangChain callbacks intentionally do not attach their spans to the
            # Python runtime context. Link SDK spans to that active chain instead.
            if span.parent is not None or importlib.util.find_spec("langchain_core") is None:
                return
            try:
                from openinference.instrumentation.langchain import get_current_span
                parent = get_current_span()
                if parent is not None and parent.get_span_context().is_valid:
                    span.add_link(parent.get_span_context(), {"observatory.link.reason": "langchain.runtime_context"})
            except Exception:
                pass

    provider.add_span_processor(FrameworkLinks())

    class LiveProgress(SpanProcessor):
        def _send(self, span, stage):
            scope = getattr(span, "instrumentation_scope", None)
            if scope is None or scope.name not in {"openinference.instrumentation.openai", "openinference.instrumentation.anthropic"}:
                return
            event = {"trace_id": f"{span.context.trace_id:032x}", "stage": stage,
                     "message": f"{service} · {span.name}", "service": service}
            try:
                events.put_nowait(event)
            except queue.Full:
                pass

        def on_start(self, span, parent_context=None):
            self._send(span, "Model call started")

        def on_end(self, span):
            self._send(span, "Model call finished")

    provider.add_span_processor(LiveProgress())
    trace.set_tracer_provider(provider)
    config = TraceConfig(hide_inputs=not capture_content, hide_outputs=not capture_content,
                         hide_input_images=True, hide_embeddings_vectors=True,
                         hide_embeddings_text=not capture_content,
                         hide_llm_invocation_parameters=not capture_content)
    enabled, failures, instrumentors = [], [], []
    connection = None
    original_argv, original_path = sys.argv[:], sys.path[:]
    try:
        for sdk, name, class_name in INTEGRATIONS:
            if importlib.util.find_spec(sdk) is None:
                continue
            try:
                package = importlib.import_module(f"openinference.instrumentation.{name}")
                instrumentor = getattr(package, class_name)()
                for dependency in instrumentor.instrumentation_dependencies():
                    required = Requirement(dependency)
                    installed = importlib.metadata.version(required.name)
                    if installed not in required.specifier:
                        raise SetupError(f"requires {required}; found {installed}. Use a matching integration version or update that SDK.")
                instrumentor.instrument(tracer_provider=provider, config=config)
                if not instrumentor.is_instrumented_by_opentelemetry:
                    raise SetupError("SDK and instrumentor versions do not match")
                instrumentors.append(instrumentor)
                enabled.append(name)
            except Exception as exc:
                failures.append(f"{name}: {exc}")
        if not enabled:
            hint = " ".join(failures) if failures else "No supported SDK found."
            raise SetupError("Automatic capture needs the OpenAI SDK, Anthropic SDK, or LangChain in this same Python environment. " + hint)
        if failures:
            print("Goechala: some integrations could not start: " + ", ".join(failures), file=sys.stderr)
        connection = _Connection(endpoint, service, enabled, capture_content, token, source_id, events)
        connection.start()
        print(f"Goechala is listening to {service}: {', '.join(enabled)}.\nReceiver: {endpoint}", file=sys.stderr)
        sys.argv = [target, *arguments]
        if module:
            runpy.run_module(target, run_name="__main__", alter_sys=True)
        else:
            sys.path.insert(0, str(path.parent))
            runpy.run_path(str(path), run_name="__main__")
    finally:
        # Finished streaming calls are exported before the connection is marked stopped.
        provider.shutdown()
        if connection is not None:
            connection.close()
        for instrumentor in reversed(instrumentors):
            instrumentor.uninstrument()
        sys.argv, sys.path = original_argv, original_path
