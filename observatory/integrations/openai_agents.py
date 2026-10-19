"""OpenAI Agents SDK tracing exporter for Observatory."""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from typing import Any

try:
    from agents.tracing.processor_interface import TracingExporter
except ImportError as exc:  # pragma: no cover - optional integration dependency
    raise ImportError(
        "OpenAIAgentsExporter requires the optional dependency: "
        "pip install 'agent-observatory-cli[openai-agents]'"
    ) from exc


_SAFE_TEXT_KEYS = {"type", "name", "model", "model_name", "model_id", "provider",
                   "agent_name", "tool_name", "handoff_name", "operation"}
_TOKEN_KEYS = {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
               "total_tokens", "cached_input_tokens", "cache_read_input_tokens",
               "cache_creation_input_tokens", "cache_write_input_tokens",
               "reasoning_tokens", "reasoning_output_tokens"}


def _stable_id(value: Any, length: int) -> str:
    """Convert SDK IDs (which may be prefixed) to valid, stable hex IDs."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]


def _timestamp_ns(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000_000)
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_attributes(data: Any, span_kind: str, exported=None) -> dict[str, Any]:
    """Pick identifying fields and token counts; omit inputs, outputs, and prompts."""
    if exported is None:
        exported = data.export() if callable(getattr(data, "export", None)) else {}
    if not isinstance(exported, Mapping):
        return {}
    result: dict[str, Any] = {}

    def visit(value: Any, prefix: str = "") -> None:
        if not isinstance(value, Mapping):
            return
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.lower()
            if isinstance(item, Mapping):
                visit(item, f"{prefix}{key}.")
            elif normalized in _SAFE_TEXT_KEYS and isinstance(item, (str, int, float, bool)):
                if normalized in {"model", "model_name", "model_id"}:
                    result.setdefault("gen_ai.request.model", item)
                elif normalized == "provider":
                    result["gen_ai.provider.name"] = item
                elif normalized == "agent_name" or (normalized == "name" and span_kind == "agent"):
                    result["gen_ai.agent.name"] = item
                elif normalized == "name" and span_kind == "function":
                    result["tool.name"] = item
                elif normalized == "tool_name":
                    result["tool.name"] = item
                else:
                    result[f"openai_agents.{prefix}{key}"] = item
            elif (span_kind == "generation" and normalized in _TOKEN_KEYS and
                  isinstance(item, (int, float)) and not isinstance(item, bool) and item >= 0):
                token_name = {
                    "prompt_tokens": "input_tokens", "completion_tokens": "output_tokens",
                    "cached_input_tokens": "cache_read.input_tokens",
                    "cache_read_input_tokens": "cache_read.input_tokens",
                    "cache_creation_input_tokens": "cache_write.input_tokens",
                    "cache_write_input_tokens": "cache_write.input_tokens",
                    "reasoning_output_tokens": "reasoning_tokens",
                }.get(normalized, normalized)
                result[f"gen_ai.usage.{token_name}"] = int(item)

    visit(exported)
    return result


def _bounded_content(value: Any, max_chars: int) -> str:
    if isinstance(value, str):
        content = value
    else:
        content = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(content) > max_chars:
        return content[:max_chars] + "… [truncated]"
    return content


class OpenAIAgentsExporter(TracingExporter):
    """Export OpenAI Agents SDK spans to an Observatory :class:`Client`.

    Pass this exporter to the SDK's ``BatchTraceProcessor`` and register that
    processor with ``agents.tracing.add_trace_processor``. Span input/output
    payloads and arbitrary trace metadata are intentionally omitted.
    """

    def __init__(self, client, *, capture_content=False, max_content_chars=4000):
        self.client = client
        self._trace_context: OrderedDict[str, tuple[str, str | None]] = OrderedDict()
        self.capture_content = bool(capture_content)
        self.max_content_chars = max(1, min(100_000, int(max_content_chars)))

    def export(self, items) -> None:
        spans = []
        for item in items:
            if hasattr(item, "span_data"):
                spans.append(self._span_payload(item))
            elif hasattr(item, "trace_id") and hasattr(item, "name"):
                trace_id = str(item.trace_id)
                group_id = getattr(item, "group_id", None)
                self._trace_context[trace_id] = (str(item.name or "agent.run"),
                                                 str(group_id) if group_id else None)
                self._trace_context.move_to_end(trace_id)
                while len(self._trace_context) > 4096:
                    self._trace_context.popitem(last=False)
        if spans:
            post_spans = getattr(self.client, "_post_spans", None)
            if callable(post_spans):
                post_spans(spans)
            else:
                self.client._post("/api/spans", {"service": self.client.service, "spans": spans})

    def _span_payload(self, span):
        raw_trace_id = str(span.trace_id)
        raw_span_id = str(span.span_id)
        raw_parent_id = span.parent_id
        data = span.span_data
        kind = getattr(data, "type", None) or type(data).__name__.removesuffix("SpanData").lower()
        exported = data.export() if callable(getattr(data, "export", None)) else {}
        attrs = _safe_attributes(data, str(kind), exported)
        if self.capture_content and isinstance(exported, Mapping):
            for field in ("input", "output"):
                if exported.get(field) is not None:
                    attrs[f"openai_agents.{field}"] = _bounded_content(
                        exported[field], self.max_content_chars)
        attrs["openai_agents.trace_id"] = raw_trace_id
        attrs["openai_agents.span_id"] = raw_span_id
        workflow, group_id = self._trace_context.get(raw_trace_id, ("agent.run", None))
        attrs["openai_agents.workflow"] = workflow
        if not raw_parent_id:
            attrs["agent.task"] = workflow
        if group_id:
            attrs["session.id"] = group_id
        error = span.error
        status = "ERROR" if error else "OK"
        if error:
            attrs["exception.type"] = "OpenAIAgentsError"
        payload = {
            "trace_id": _stable_id(raw_trace_id, 32),
            "span_id": _stable_id(raw_span_id, 16),
            "parent_span_id": _stable_id(raw_parent_id, 16) if raw_parent_id else None,
            "name": f"openai_agents.{kind}",
            "start_unix_nano": _timestamp_ns(span.started_at),
            "end_unix_nano": _timestamp_ns(span.ended_at),
            "status": status,
            "attributes": attrs,
            "events": [],
            "resource_attributes": self.client.resource_attributes,
        }
        if payload["start_unix_nano"] <= 0:
            payload["start_unix_nano"] = payload["end_unix_nano"]
        payload["end_unix_nano"] = max(payload["start_unix_nano"], payload["end_unix_nano"])
        return payload


def create_processor(client, *, capture_content=False, max_content_chars=4000,
                     **processor_options):
    """Create the SDK's background batching processor for this Observatory client."""
    try:
        from agents.tracing.processors import BatchTraceProcessor
    except ImportError as exc:  # pragma: no cover - optional integration dependency
        raise ImportError(
            "create_processor requires the optional dependency: "
            "pip install 'agent-observatory-cli[openai-agents]'"
        ) from exc
    exporter = OpenAIAgentsExporter(client, capture_content=capture_content,
                                    max_content_chars=max_content_chars)
    return BatchTraceProcessor(exporter, **processor_options)
