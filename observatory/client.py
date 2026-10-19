"""Small, dependency-free client for instrumenting Python agent code."""
from __future__ import annotations

import asyncio
import contextvars
import json
import secrets
import time
import urllib.request
from typing import Any

_ACTIVE_RUN: contextvars.ContextVar["Run | None"] = contextvars.ContextVar("observatory_run", default=None)
_ACTIVE_SPAN: contextvars.ContextVar["Span | None"] = contextvars.ContextVar("observatory_span", default=None)
_AUTO_PARENT = object()
MAX_REQUEST_BYTES = 8 * 1024 * 1024


def _random_hex(byte_count):
    value = secrets.token_hex(byte_count)
    while not any(char != "0" for char in value):
        value = secrets.token_hex(byte_count)
    return value


class Client:
    """Emit framework-neutral spans and progress to an Goechala receiver."""

    def __init__(self, endpoint="http://127.0.0.1:4318", service="python-agent",
                 token=None, timeout=3.0, strict=False, capture_exception_message=False,
                 resource_attributes=None):
        self.endpoint = endpoint.rstrip("/")
        self.service = str(service)
        self.token = token
        self.timeout = float(timeout)
        self.strict = bool(strict)
        self.capture_exception_message = bool(capture_exception_message)
        self.resource_attributes = dict(resource_attributes or {})
        self.last_error: Exception | None = None

    def trace(self, name="agent.run", *, task=None, session_id=None, attributes=None):
        return Run(self, name, task, session_id, attributes or {})

    def _post_spans(self, spans):
        base = {"service": self.service, "spans": []}
        base_size = len(json.dumps(base, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        batch = []
        batch_size = base_size
        successful = True
        last_error = None

        def send():
            nonlocal successful, last_error, batch, batch_size
            if not batch:
                return
            if not self._post("/api/spans", {"service": self.service, "spans": batch}):
                successful = False
                last_error = self.last_error
            batch = []
            batch_size = base_size

        for span in spans:
            try:
                item_size = len(json.dumps(span, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            except Exception as exc:
                self.last_error = exc
                if self.strict:
                    raise
                successful = False
                last_error = exc
                continue
            if base_size + item_size > MAX_REQUEST_BYTES:
                exc = ValueError("a span exceeds the receiver's 8 MiB request limit")
                self.last_error = exc
                if self.strict:
                    raise exc
                successful = False
                last_error = exc
                continue
            separator = 1 if batch else 0
            if batch and batch_size + separator + item_size > MAX_REQUEST_BYTES:
                send()
                separator = 0
            batch.append(span)
            batch_size += separator + item_size
        send()
        if not successful:
            self.last_error = last_error
        return successful

    def _post(self, path, payload):
        try:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            request = urllib.request.Request(self.endpoint + path, data=data,
                                             headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise OSError(f"Goechala returned HTTP {response.status}")
            return True
        except Exception as exc:
            self.last_error = exc
            if self.strict:
                raise
            return False


class Run:
    def __init__(self, client, name, task, session_id, attributes):
        self.client = client
        self.trace_id = _random_hex(16)
        self.session_id = str(session_id).strip() if session_id is not None else None
        if self.session_id is not None and (not self.session_id or len(self.session_id) > 512):
            raise ValueError("session_id must contain 1 to 512 characters")
        self.spans: list[Span] = []
        root_attributes = dict(attributes)
        if task is not None:
            root_attributes.setdefault("agent.task", str(task))
        if self.session_id:
            root_attributes.setdefault("session.id", self.session_id)
        self.root = Span(self, name, root_attributes, parent_span_id=None)
        self.spans.append(self.root)
        self._run_token = None
        self._closed = False

    def __enter__(self):
        if self._closed or self._run_token is not None:
            raise RuntimeError("run cannot be entered more than once")
        self._run_token = _ACTIVE_RUN.set(self)
        self.root.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._finish(exc_type, exc, tb)
        self._flush()
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb):
        self._finish(exc_type, exc, tb)
        await asyncio.to_thread(self._flush)
        return False

    def _finish(self, exc_type, exc, tb):
        if self._closed:
            return
        self.root.__exit__(exc_type, exc, tb)
        if self._run_token is not None:
            _ACTIVE_RUN.reset(self._run_token)
            self._run_token = None
        self._closed = True

    def span(self, name, *, attributes=None, parent_span_id=_AUTO_PARENT):
        if self._closed:
            raise RuntimeError("run is already closed")
        if parent_span_id is _AUTO_PARENT:
            active = _ACTIVE_SPAN.get()
            parent_span_id = active.span_id if active is not None and active.run is self else self.root.span_id
        span = Span(self, name, attributes or {}, parent_span_id=parent_span_id)
        self.spans.append(span)
        return span

    def progress(self, stage, message="", percent=None):
        if self._closed:
            raise RuntimeError("run is already closed")
        payload = {"trace_id": self.trace_id, "stage": str(stage), "message": str(message), "service": self.client.service}
        if percent is not None:
            payload["percent"] = percent
        if self.session_id:
            payload["session_id"] = self.session_id
        return self.client._post("/api/progress", payload)

    async def aprogress(self, stage, message="", percent=None):
        return await asyncio.to_thread(self.progress, stage, message, percent)

    def _flush(self):
        payload = []
        for span in self.spans:
            payload.append({"trace_id": self.trace_id, "span_id": span.span_id,
                            "parent_span_id": span.parent_span_id, "name": span.name,
                            "start_unix_nano": span.start_ns, "end_unix_nano": span.end_ns,
                            "status": span.status, "attributes": span.attributes,
                            "events": span.events,
                            "resource_attributes": self.client.resource_attributes})
        return self.client._post_spans(payload)


class Span:
    def __init__(self, run, name, attributes, parent_span_id):
        self.run = run
        self.name = str(name)
        self.span_id = _random_hex(8)
        self.parent_span_id = parent_span_id
        self.start_ns = time.time_ns()
        self.end_ns = self.start_ns
        self.status = "UNSET"
        self.attributes: dict[str, Any] = dict(attributes)
        self.events: list[dict[str, Any]] = []
        self._context_token = None
        self._finished = False

    def __enter__(self):
        if self._context_token is not None or self._finished:
            raise RuntimeError("span cannot be entered more than once")
        self._context_token = _ACTIVE_SPAN.set(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.finish(exc)
        if self._context_token is not None:
            _ACTIVE_SPAN.reset(self._context_token)
            self._context_token = None
        return False

    def finish(self, error=None):
        if self._finished:
            return self
        if error is not None:
            self.status = "ERROR"
            self.attributes.setdefault("exception.type", type(error).__name__)
            exception = {"type": type(error).__name__}
            if self.run.client.capture_exception_message:
                message = str(error)
                self.attributes.setdefault("exception.message", message)
                exception["message"] = message
            self.add_event("exception", exception)
        self.end_ns = max(time.time_ns(), self.start_ns)
        self._finished = True
        return self

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb):
        return self.__exit__(exc_type, exc, tb)

    def set_attribute(self, key, value):
        self.attributes[str(key)] = value
        return self

    def add_event(self, name, attributes=None):
        self.events.append({"name": str(name), "time_unix_nano": time.time_ns(),
                            "attributes": dict(attributes or {})})
        return self

    def set_status(self, status):
        if status not in {"UNSET", "OK", "ERROR"}:
            raise ValueError("status must be UNSET, OK, or ERROR")
        self.status = status
        return self
