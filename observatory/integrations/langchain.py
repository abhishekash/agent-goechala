"""LangChain and LangGraph callback adapter for Goechala traces."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from langchain_core.callbacks import BaseCallbackHandler
except ImportError as exc:  # pragma: no cover - depends on optional integration extra
    raise ImportError(
        "LangChainCallbackHandler requires the optional dependency: "
        "pip install 'agenturm[langchain] @ git+https://github.com/abhishekash/agenturm'"
    ) from exc


class LangChainCallbackHandler(BaseCallbackHandler):
    """Capture chain, tool, retriever, and model callbacks inside one active run.

    Attach an instance to a :class:`observatory.client.Run` before invoking a
    LangChain runnable or LangGraph app. Prompt, tool input, and output content
    are omitted unless ``capture_content=True``.
    """

    def __init__(self, *, capture_content=False, max_content_chars=1000):
        super().__init__()
        self.capture_content = bool(capture_content)
        self.max_content_chars = max(1, int(max_content_chars))
        self.run = None
        self._spans = {}

    def attach(self, run):
        if self.run is not None:
            raise RuntimeError("callback handler is already attached to a run")
        if run._closed:
            raise RuntimeError("cannot attach to a closed Goechala run")
        self.run = run
        return self

    def _content(self, value):
        return str(value)[:self.max_content_chars]

    @staticmethod
    def _run_key(run_id):
        return str(run_id) if run_id is not None else None

    @staticmethod
    def _name(serialized, kwargs, fallback):
        return str((serialized or {}).get("name") or kwargs.get("name") or fallback)

    @staticmethod
    def _model(serialized, kwargs):
        values = [kwargs.get("invocation_params"), (serialized or {}).get("kwargs"), kwargs]
        for mapping in values:
            if not isinstance(mapping, Mapping):
                continue
            for key in ("model", "model_name", "model_id", "deployment_name"):
                value = mapping.get(key)
                if value:
                    return str(value)
        return None

    def _start(self, kind, name, run_id, parent_run_id=None, attributes=None):
        if self.run is None:
            return None
        parent = self._spans.get(self._run_key(parent_run_id))
        parent_span_id = parent.span_id if parent else self.run.root.span_id
        attrs = {"langchain.run_type": kind}
        attrs.update(attributes or {})
        span = self.run.span(name, attributes=attrs, parent_span_id=parent_span_id)
        key = self._run_key(run_id)
        if key is not None:
            self._spans[key] = span
        return span

    def _finish(self, run_id, error=None, attributes=None):
        span = self._spans.get(self._run_key(run_id))
        if span is None:
            return
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        span.finish(error)

    def _capture_output(self, span, output):
        if self.capture_content and span is not None:
            span.set_attribute("langchain.output", self._content(output))

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None,
                       tags=None, metadata=None, **kwargs):
        attrs = {}
        if tags:
            attrs["langchain.tags"] = [str(tag) for tag in tags]
        if self.capture_content:
            attrs["langchain.input"] = self._content(inputs)
        self._start("chain", self._name(serialized, kwargs, "chain"),
                    run_id, parent_run_id, attrs)

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        span = self._spans.get(self._run_key(run_id))
        self._capture_output(span, outputs)
        self._finish(run_id)

    def on_chain_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, error)

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None,
                      tags=None, metadata=None, **kwargs):
        name = self._name(serialized, kwargs, "tool")
        attrs = {"tool.name": name}
        if self.capture_content:
            attrs["tool.input"] = self._content(input_str)
        self._start("tool", f"tool.{name}", run_id, parent_run_id, attrs)

    def on_tool_end(self, output, *, run_id, **kwargs):
        span = self._spans.get(self._run_key(run_id))
        self._capture_output(span, output)
        self._finish(run_id)

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, error)

    def on_retriever_start(self, serialized, query, *, run_id, parent_run_id=None,
                           tags=None, metadata=None, **kwargs):
        attrs = {}
        if self.capture_content:
            attrs["retrieval.query"] = self._content(query)
        self._start("retriever", self._name(serialized, kwargs, "retriever.search"),
                    run_id, parent_run_id, attrs)

    def on_retriever_end(self, documents, *, run_id, **kwargs):
        span = self._spans.get(self._run_key(run_id))
        if span is not None:
            span.set_attribute("retrieval.document_count", len(documents))
        self._finish(run_id)

    def on_retriever_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, error)

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None,
                     tags=None, metadata=None, **kwargs):
        attrs = {"gen_ai.operation.name": "text_completion"}
        model = self._model(serialized, kwargs)
        if model:
            attrs["gen_ai.request.model"] = model
        if self.capture_content:
            attrs["langchain.input"] = self._content(prompts)
        self._start("llm", "llm.call", run_id, parent_run_id, attrs)

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None,
                            tags=None, metadata=None, **kwargs):
        attrs = {"gen_ai.operation.name": "chat"}
        model = self._model(serialized, kwargs)
        if model:
            attrs["gen_ai.request.model"] = model
        if self.capture_content:
            attrs["langchain.input"] = self._content(messages)
        self._start("llm", "chat.call", run_id, parent_run_id, attrs)

    @staticmethod
    def _token_usage(response):
        generations = getattr(response, "generations", None) or []
        totals = {"input_tokens": 0, "output_tokens": 0}
        found = False
        for group in generations:
            for generation in group[:1]:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if isinstance(usage, Mapping):
                    for key in totals:
                        value = usage.get(key)
                        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                            totals[key] += int(value)
                            found = True
        if found:
            return totals
        output = getattr(response, "llm_output", None) or {}
        if not isinstance(output, Mapping):
            return {}
        usage = output.get("token_usage") or output.get("usage") or {}
        if not isinstance(usage, Mapping):
            return {}
        return {"input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
                "output_tokens": usage.get("output_tokens", usage.get("completion_tokens"))}

    def on_llm_end(self, response, *, run_id, **kwargs):
        span = self._spans.get(self._run_key(run_id))
        if span is None:
            return
        usage = self._token_usage(response)
        for source, target in (("input_tokens", "gen_ai.usage.input_tokens"),
                               ("output_tokens", "gen_ai.usage.output_tokens")):
            value = usage.get(source)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                span.set_attribute(target, int(value))
        if self.capture_content:
            self._capture_output(span, response)
        self._finish(run_id)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, error)

    def on_agent_action(self, action, *, run_id, **kwargs):
        span = self._spans.get(self._run_key(run_id)) or (self.run.root if self.run else None)
        if span is not None:
            attrs = {"tool.name": str(getattr(action, "tool", "agent.action"))}
            if self.capture_content:
                attrs["tool.input"] = self._content(getattr(action, "tool_input", ""))
            span.add_event("agent.action", attrs)

    def on_agent_finish(self, finish, *, run_id, **kwargs):
        span = self._spans.get(self._run_key(run_id)) or (self.run.root if self.run else None)
        if span is not None:
            span.add_event("agent.finish")

    def on_custom_event(self, name, data, *, run_id=None, **kwargs):
        span = self._spans.get(self._run_key(run_id)) or (self.run.root if self.run else None)
        event_name = str(name)
        if span is not None:
            attrs = {"event.name": event_name}
            if self.capture_content:
                attrs["event.data"] = self._content(data)
            span.add_event("langchain.custom", attrs)
        if self.run is None or event_name.lower() not in {
                "progress", "agent.progress", "langgraph.progress"}:
            return
        progress = data if isinstance(data, Mapping) else {}
        stage = progress.get("stage") or progress.get("agent.progress.stage") or "agent"
        message = progress.get("message") or ""
        percent = progress.get("percent", progress.get("agent.progress.percent"))
        self.run.progress(str(stage), str(message), percent)

    def on_retry(self, retry_state, *, run_id, **kwargs):
        span = self._spans.get(self._run_key(run_id))
        if span is not None:
            span.add_event("langchain.retry")
