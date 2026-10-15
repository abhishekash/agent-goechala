## 0.1.2 · Native session status, MCP inventory, optional AI summary

Shows agent and session IDs, repo/project, lifecycle status, update age, pending wait, and task details. MCP server calls now link across start/result events, expose qualified Claude Code and Codex tool names, report completion/error status, and include all stored history in server counts. Optional provider-backed session summaries run only when requested. DuckDB guidance is in [the storage evaluation](decisions/duckdb.md).

# Agent Observatory

A local, framework-independent receiver and investigation dashboard for AI agents. It accepts **OTLP/HTTP traces, logs, and metrics in protobuf or JSON** from OpenTelemetry-instrumented runtimes and the existing **agent-harness JSONL** format. A single trace view shows parent-linked spans, span links, correlated logs, progress, model usage, latency, known cost, and evidence-linked findings. The metric explorer charts one instrument at a time, keeping kind, temporality, scope, unit, resource, and attribute series separate; it plots gauge/sum values and lets you select distribution count, sum, min/max, summary quantiles, or one latest explicit/exponential histogram bucket distribution with its source attributes shown. Data lives in SQLite. The receiver uses Python's standard library (3.11+); the terminal UI uses Textual.

## Install and launch the terminal cockpit

Python 3.11+ is required. Install the CLI in an isolated environment with [uv tools](https://docs.astral.sh/uv/guides/tools/):

```bash
uv tool install agent-observatory-cli
agent-observatory
```

If uv is not installed, use its [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/). If the installed command is missing from your PATH, run `uv tool update-shell` and open a new terminal. You can also install into an active Python environment with `python -m pip install agent-observatory-cli`.

The cockpit opens a tabbed interface for Agents, Activity, MCP, Traces and Host, plus Output when it launches an agent. The Agents view shows individual sessions, tasks, models and recent work. Session counts and live process counts are separate; approval reviews are hidden by default. It attaches to a receiver on **127.0.0.1:14318**, or starts one there. Only this localhost listener is needed. Run `agent-observatory` for full-screen panels in your current terminal. From this checkout, `python3 -m observatory` also works without installation.

### Existing Codex and Claude Code agents

Local receivers automatically discover recent session files under `~/.codex/sessions` and `~/.claude/projects` (`CODEX_HOME` and `CLAUDE_CONFIG_DIR` are respected). Start the cockpit with **no agent filename or Python environment flags**:

```bash
agent-observatory
```

It reads actual logged activity as files update, including already running sessions. Agents need no restart or configuration change. Source logs stay untouched. The terminal has a live activity panel with an inspector; the web dashboard has agent task cards, a searchable activity feed, category filters, a detail inspector, and links to the related trace.

| Observed data | What appears |
| --- | --- |
| Tasks and messages | User requests, assistant updates and answers, current task per session |
| Model calls | Model/provider, reported input/output/cache tokens, linked turn, known cost when reported |
| Tools | Tool names, inputs, results, MCP server, errors and available duration |
| Commands | Shell command, working directory, exit code, captured stdout/stderr |
| Files | Changed paths, operation, recorded patch/diff |
| Browser | Searches, URLs, actions, recorded result metadata |
| Agent lifecycle | Turn start/completion/interruption, context compaction, processing markers |
| Context | Project, selected model, reasoning effort and recorded sandbox/approval settings |
| Runtime | Host/OS/architecture, load average, known local agent PIDs, state, RAM and cumulative CPU on Linux |

Native session capture includes bounded text and content by default. Recognizable credentials and sensitive structured fields are redacted before storage; this is best effort. Opaque/encrypted reasoning, private reasoning text, binary payloads, process environments and credential files are excluded. Content is limited to 8,000 characters per activity record, with nested fields bounded to 2,000 characters and at most 60 entries. Source schemas and upstream truncation can leave gaps. Use `serve --metadata-only` or `cockpit --metadata-only` for a receiver that omits native message text, commands and results. This setting controls future ingestion and does not erase previously stored content.

Connections show **RECENT** when a file changed within 35 seconds and **IDLE** otherwise; these describe observed file activity. Process inventory is reported separately and is not an inferred session-to-process mapping. Token usage comes from records; input counts include repeated/cached context and are not a billable-cost estimate. Costs remain unknown when not reported. Model-call latency is marked unknown when the log has no start time.

Discovery scans every 10 seconds and tails discovered files every second. It selects up to 50 files modified within the past 24 hours, importing at most the last 8 MiB initially per file. Summaries may therefore cover only part of a long session. Codex response IDs and Claude message IDs prevent duplicate usage entries on reread; older Codex cumulative token snapshots are handled separately. A version with a different log schema may expose less activity. `GET /api/health` reports discovery status, session/task snapshots, runtime metadata, source count, read errors, and skipped malformed records. `GET /api/activity?limit=100&service=codex&domain=shell&q=...` exposes a bounded, searchable activity view from the latest 100 observed traces. The web feed shows up to 150 events; the terminal shows up to 100, with full trace investigation available separately.

Local discovery runs in the receiver. If you installed an earlier version, reinstall the CLI and restart that receiver to enable it. A cockpit attached to an older receiver cannot enable discovery there. Add `--no-local-agents` to `serve` or `cockpit` to disable discovery in a receiver started by that command. Receivers bound to non-loopback addresses do not discover local sessions automatically. Other agent products still need an adapter or OTLP export.

### Launch a Python SDK agent with the panels

Automatic capture must run in the agent's own Python environment. From your agent project, install the extra into that environment:

```bash
uv pip install --python .venv/bin/python 'agent-observatory-cli[auto]'
agent-observatory cockpit --python .venv/bin/python --service my-agent agent.py
```

Replace the virtual environment path, service name, and agent filename with your own. The agent's existing dependencies remain in its environment. Arguments after `agent.py` go to the script; `--module` runs a Python module. If Observatory is already installed in the active agent environment, use `python -m observatory cockpit --service my-agent agent.py`.

Other already running processes need to export OTLP telemetry to this receiver or be restarted through the capture launcher. The output panel captures stdout/stderr; use this launcher for agents that do not require interactive stdin. Python SDK prompts and responses are omitted unless you add `--capture-content`. Native session capture has its own `--metadata-only` receiver setting.

Use the mouse or **1–5** to switch views. **↑/↓** selects a table row; its inspector updates automatically. **Enter** opens the inspector; **e** expands it to use the full workspace and **Esc** returns to the list. On compact terminals, Enter expands the inspector automatically. **Tab** moves between controls and scrollable panels. On Agents, **s** focuses the selected session's activity. **t** opens the selected activity or MCP call's related trace and execution span. **Alt+Left** or the **Back** button returns to the original event and scroll position; **Esc** first clears a text selection or closes an expanded inspector, then returns from a linked trace. **/** focuses the current view's search, **Esc** returns to its table, **a** resets session and search filters, **v** toggles approval reviews, **p** pauses updates, **b** opens the web dashboard, and **q** quits. When search has focus, printable keys enter text; press **Esc** before using shortcuts.

Each view has its own search field and result count directly above its rows. Search in Agents filters sessions by task, project, model, runtime, or session ID. Activity, MCP Calls, and Traces query their own records; Host filters the process list. Filters stay with their view when switching tabs. A task search in Agents does not hide MCP calls or reduce the selected agent's recent-work feed. **Clear** resets that view's query; the session Scope control applies across views. Overview counters retain the inventory for the selected session scope. The **Errors** button in Activity and MCP Calls filters failures within the current results; it does not change the inventory counts. **a** also resets these error filters. Tables resize their columns to the terminal, preserve the selected record, and keep latency and status visible. Inspectors wrap long text to the panel width.

MCP shows reported server/tool, state, duration, arguments/results and trace/span IDs. The trace view shows a parent-linked span tree; unknown durations remain “not reported”. “Error” describes recorded errors within a trace; “Active · errors” means a logged active turn also contains an error. Agent “Recent/Idle” states describe observed session activity. Live process counts exclude zombie/exited states and do not claim a session-to-PID mapping.

The view polls about once a second and preserves the event being read as new records arrive. Activity, MCP Calls, and Traces have **Older**, **Newer**, and **Latest** controls (shortcuts **]**, **[**, and **l**) to browse saved records in pages of 200 events, 100 MCP calls, or 100 traces. Search and scope changes return that view to its latest matching page. Activity history includes saved logs beyond a trace's preview window. The selected trace initially shows 100 spans; related-trace navigation also loads its exact target span and up to eight missing ancestors. MCP overview counts cover the latest loaded operations in scope. Global token totals include stored history and repeated/cached context; costs remain unreported when telemetry has no price. The web dashboard supports deeper span paging. History controls require the updated receiver; if a separate receiver is already running, restart it after updating.

Drag across inspector text to highlight it, then press **Ctrl+C** (or **Cmd+C** where the terminal forwards it) to copy the selected plain text. Display updates are held while text is selected; click or press **Esc** to clear the selection and resume. **c** copies the selection, or the current event/call/details when nothing is selected. The Activity and MCP inspectors also have copy buttons. Clipboard commands run in the background and use Wayland, X11, macOS, or Windows tools when available, falling back to a terminal clipboard request. When using a remote terminal, clipboard behavior depends on that terminal's support. Narrow terminals stack the list and inspector; **e** expands the inspector for reading.

`GET /api/activity?limit=200&cursor=...&q=...` and `GET /api/mcp?limit=100&cursor=...&service=codex&session_id=...&q=...` return `next_cursor` for older pages. Keep the same filters when following a cursor. The MCP endpoint returns reported MCP operations with server/tool, duration, arguments/results and related trace/span IDs. Linked logs and spans are joined; older unlinked duplicates for the same event are omitted when a linked record exists. Distinct explicit spans remain separate, including concurrent calls sharing a timestamp.

Quitting stops an agent and receiver started by that cockpit. An existing receiver and other agents stay running. Closing the agent output does not delete stored traces. To keep a receiver running between cockpit sessions, start `python -m observatory serve --port 14318` separately.

```bash
# Connect to an existing receiver without starting one:
agent-observatory cockpit --endpoint http://127.0.0.1:14318
# Print a snapshot for shell use:
agent-observatory cockpit --endpoint http://127.0.0.1:14318 --once
# Reinstall after updating this checkout:
uv tool install --reinstall ./agent-observatory
```

The current terminal is the default. Add `--new-window` only when you want a separate graphical terminal (Terminal on macOS; GNOME Terminal, Konsole, or x-terminal-emulator/xterm on Linux; Windows Terminal on Windows). In SSH or headless sessions, use the default. The Textual interface supports Linux, macOS and Windows terminals without a curses dependency.

## Connect an agent without changing its code

Open the dashboard and choose **Connect an agent**. The guide provides a downloadable installer, fills in the receiver address, lets you name your agent, and gives you install and launch commands. Download it and run the install command from your download folder in the agent’s environment; the package works outside this checkout. It shows whether the launcher is connected and how many spans that process has sent.

For example, start a persistent local receiver on port 14318:

```bash
python3 -m observatory --db ./observatory.db serve --port 14318
```

Activate the Python environment your agent uses. From this checkout, install the automatic capture extra once:

```bash
python -m pip install '.[auto]'
```

From your agent's folder, replace `python agent.py` with:

```bash
python -m observatory run --endpoint http://127.0.0.1:14318 --service my-agent agent.py
```

Your script and its SDK calls run normally. The launcher detects installed OpenAI, Anthropic, and LangChain SDKs, attaches [OpenInference instrumentation](https://github.com/Arize-ai/openinference/tree/main/python), and sends completed spans through OpenTelemetry to the local receiver. SDK versions must match the installed instrumentors; the launcher reports dependency mismatches before starting an unsupported setup. OpenAI-compatible endpoints such as local model servers are supported when your agent calls them through the OpenAI Python SDK. LangChain / LangGraph callbacks provide framework activity, and SDK calls inside a chain are linked to its trace when the framework does not attach runtime context.

Arguments after the filename are passed to your agent. Use `--module` before the target to run a package instead of a file. The launcher uses the active Python environment; install the auto extra in that environment. Restart existing agents through the launcher to enable capture. For other runtimes, use their OpenTelemetry export support or the manual clients below.

By default, automatic capture includes model metadata, reported tokens, duration, and errors. Select **Include prompts and responses** in the guide, or add `--capture-content`, to capture content too. Captured string attributes are limited to 4,000 characters. Token usage depends on what the model SDK returns; OpenAI chat streams need `stream_options={"include_usage": True}` for token counts. Costs remain unknown unless telemetry reports a cost.

Set `OBSERVATORY_TOKEN` in the agent's terminal when the receiver requires bearer authentication. The launcher checks the receiver before starting, sends a heartbeat while the agent runs, reports model-call start/finish progress, and flushes completed spans before marking the agent finished. Active model calls appear as RUNNING before their completed spans arrive, including when filtering by agent service. Each process has its own source ID, so connection counters distinguish agents even when they share a service name. An abrupt process exit becomes disconnected after its heartbeat expires.

To try automatic capture without API keys or paid model calls, the local replay example uses real SDK clients with in-memory responses:

```bash
python -m pip install '.[auto]' 'openai>=3' 'anthropic>=1.8' langchain-core
python -m observatory run --endpoint http://127.0.0.1:14318 --service local-capture-demo examples/automatic_capture_demo.py
```

The replay uses the current SDKs' `httpx2` mock transport and records an OpenAI call inside a LangChain runnable, an async OpenAI stream, an Anthropic message, and an expected error. All model responses are local replay data. Telemetry goes only to your selected Observatory receiver.

## Run

```bash
cd agent-observatory
python3 -m observatory --db ./observatory.db serve
# Open http://127.0.0.1:4318
```

Set a retention window for long-running local deployments. The server prunes at startup and hourly; complete trace data is removed together, while unlinked logs, metric points, and endpoint inventory rows use their own timestamps:

```bash
python3 -m observatory --db ./observatory.db serve --retention-days 30
# Or run a one-time cleanup:
python3 -m observatory --db ./observatory.db prune --older-than-days 30
```

Import an existing harness trace:

```bash
python3 -m observatory --db ./observatory.db import-jsonl ../agent-harness/examples/demo_trace.jsonl
```

Join evaluation outcomes to their trace IDs:

```bash
python3 -m observatory --db ./observatory.db import-evals /path/to/results.json
```

The importer reads the `results` array written by `agent-evals`, including the offline LangGraph control in this workspace. An outcome that fails its scorer appears as `FAILED` even when every execution span has status `OK`. Other runtimes can post the same fields (`trace_id`, boolean `passed`, optional `task_id`, `scores`, and `error`) to `/api/outcomes`.

To watch a live harness run, pass its trace file to the server. New finished spans appear in the dashboard within about five seconds:

```bash
python3 -m observatory --db ./observatory.db serve --watch-jsonl /path/to/live-trace.jsonl
```

For a shared local relay, start the [OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib) distribution with `otelcol-contrib --config ./examples/otel-collector-config.yaml`. This sample accepts OTLP/gRPC on `127.0.0.1:4317` and OTLP/HTTP on `127.0.0.1:4319`, detects host name, architecture, and OS attributes, then batches traces, logs, and metrics to Observatory on port 4318. Point SDKs at the relay with `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317` and `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`, or use the HTTP listener with `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4319` and `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`. The sample binds to loopback; change the receiver endpoints only when the network path is protected.

Send OTLP/HTTP traces from another agent runtime to `http://127.0.0.1:4318/v1/traces`, logs to `/v1/logs`, and metrics to `/v1/metrics`. Standard HTTP/protobuf exporters use `Content-Type: application/x-protobuf`; HTTP/JSON exporters use `application/json`. Gzip request bodies are accepted. For a typical SDK, set `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318` and `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`. The receiver keeps trace parent IDs, attributes, events, links, resource attributes, instrumentation scope name, and status. Its endpoint inventory groups spans, logs, and metrics by `host.id`, `host.name`, `device.id`, or `service.instance.id` when producers include those resource attributes; it reports OS, architecture, user, environment, services, last seen time, and linked trace counts. Logs with a valid trace ID appear alongside that trace; `agent.progress` log events with `agent.progress.stage` and optional `agent.progress.percent` attributes also appear in its progress list; `event.name` and the equivalent `progress.*` fields are accepted as aliases. Gauge, sum, histogram, exponential histogram, and summary metric points retain their type, timestamp, attributes, unit, and available value or distribution fields. `service.name` identifies each producer. Token counts use `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`, the harness's `llm.usage.*` fields, and OpenInference `llm.token_count.prompt` / `llm.token_count.completion`; OpenInference LLM and embedding model names, indexed chat messages, and embedding input/vector summaries are also recognized. Cost totals accept `llm.cost.total`, the prompt/completion cost components, and the existing harness aliases. Unknown prices are shown as unknown, rather than zero cost. See the [OTLP exporter protocol configuration](https://opentelemetry.io/docs/languages/sdk-configuration/otlp-exporter/) for SDK settings.

Tool spans with `gen_ai.operation.name=execute_tool` and `gen_ai.tool.name` (or common `tool.name` attributes) appear automatically in the activity ledger. OpenInference spans with `openinference.span.kind=TOOL` are included as tool activity too. For file, Git, shell, network, browser, or other actions that are not already spans, emit an OpenTelemetry span event named `agent.activity` or a correlated log with event name `agent.activity`. Set `agent.activity.domain`, `agent.activity.action`, `agent.activity.subject`, and `agent.activity.result`; optionally add `agent.activity.actor`. The trace view groups the activity ledger by subject, filters events by changes or errors, and links each item back to its source span or log. Activities with result `blocked`, `denied`, or `policy_denied` also create a review finding that opens at its evidence. For example, an instrumented tool can record `span.addEvent("agent.activity", {"agent.activity.domain":"file", "agent.activity.action":"write", "agent.activity.subject":"src/app.py", "agent.activity.result":"success"})`. Keep credentials and sensitive content out of subjects and event details unless you intend to retain them.

For Python agents that need manual instrumentation, the package includes a small synchronous and asynchronous client. Nested spans inherit their parent, progress is posted while the run is active, and completed spans are sent when the run exits. Pass `resource_attributes` to identify the host in the endpoint inventory; host identity collection is opt in:

```python
import platform
import socket

from observatory import Client

telemetry = Client(
    service="research-agent",
    token="optional-bearer-token",
    resource_attributes={"host.name": socket.gethostname(), "os.type": platform.system().lower(),
                         "os.version": platform.release(), "host.arch": platform.machine()},
)

async def run_agent():
    async with telemetry.trace(task="Find current guidance", session_id="thread-42") as run:
        await run.aprogress("planning", "Choosing a search strategy", 10)
        async with run.span("tool.search", attributes={"tool.name": "search"}):
            await search_web()
        async with run.span("llm.call", attributes={"gen_ai.request.model": "my-model"}) as call:
            answer = await ask_model()
            call.set_attribute("gen_ai.usage.input_tokens", 320)
            call.set_attribute("gen_ai.usage.output_tokens", 85)
        return answer
```

The client uses the standard library, splits large traces into requests below the receiver 8 MiB limit, and is best effort by default, so telemetry delivery errors do not interrupt the agent. Set `strict=True` to raise delivery errors, or inspect `telemetry.last_error`. Exception messages are omitted by default; `capture_exception_message=True` opts in to recording them.

Node.js agents can install the dependency-free package from this repository (Node.js 18 or newer):

```bash
npm install ./clients/javascript
```

```js
import { Client } from '@agent-observatory/client';

const telemetry = new Client({
  service: 'javascript-research-agent',
  resourceAttributes: { 'host.name': 'worker-7', 'os.type': 'linux', 'host.arch': 'x86_64' },
});
const answer = await telemetry.trace(
  { task: 'Find current guidance', sessionId: 'thread-42' },
  async run => {
    await run.progress('planning', 'Choosing a search strategy', 10);
    return run.span('llm.call', { attributes: { 'gen_ai.request.model': 'my-model' } }, async call => {
      const result = await askModel();
      call.setAttribute('gen_ai.usage.input_tokens', 320);
      call.setAttribute('gen_ai.usage.output_tokens', 85);
      return result;
    });
  },
);
```

Nested spans follow asynchronous execution context, including spans started inside child callbacks. Completed spans are posted when the trace callback exits, split into requests below the receiver 8 MiB limit. As with the Python client, delivery is best effort unless `strict: true`; inspect `telemetry.lastError` to diagnose failures.

For automatic callback capture in LangChain and LangGraph, install the optional adapter:

```bash
pip install 'agent-observatory-cli[langchain]'
```

Attach the handler inside an Observatory run and pass it through the runnable config. It records nested chain, model, tool, and retriever spans; captures standardized token usage when the model supplies it; records errors; and maps LangChain custom callbacks named `progress`, `agent.progress`, or `langgraph.progress` into the run timeline. LangGraph applications that use custom stream output can call `run.aprogress(...)` as they consume those events. Prompts, tool inputs, and outputs stay out of telemetry unless `capture_content=True` is set.

```python
from observatory import Client
from observatory.integrations.langchain import LangChainCallbackHandler

telemetry = Client(service="langgraph-research-agent")

with telemetry.trace(task="Research a question", session_id="thread-42") as run:
    callbacks = LangChainCallbackHandler().attach(run)
    result = graph.invoke(question, config={"callbacks": [callbacks]})
```

For the OpenAI Agents SDK, install its optional exporter and add the batching processor to the SDK tracing provider:

```bash
pip install 'agent-observatory-cli[openai-agents]'
```

```python
from agents import Runner
from agents.tracing import flush_traces, set_trace_processors
from observatory import Client
from observatory.integrations.openai_agents import create_processor

telemetry = Client(service="support-agent")
set_trace_processors([create_processor(telemetry)])

result = await Runner.run(agent, "Help me find my order")
flush_traces()  # optional: make the spans visible immediately
```

The adapter keeps parent/child relationships and model or token metadata when available. It maps SDK IDs to stable Observatory IDs, maps trace `group_id` to Observatory session grouping, and records original trace/span IDs as attributes for correlation. Prompt, tool input/output, and arbitrary trace metadata are omitted by default. To collect span input/output, opt in with `create_processor(telemetry, capture_content=True)`; content is truncated to 4,000 characters per field by default and can be adjusted with `max_content_chars`. Token totals come from generation spans, avoiding double counting the SDK's aggregate turn usage. The SDK batching processor exports in the background; call `flush_traces()` when a job needs immediate visibility. `set_trace_processors` replaces the SDK's default OpenAI exporter. If you use `add_trace_processor` to keep that exporter too, the SDK also sends its full trace payloads to OpenAI.

For custom runtimes that cannot export OTLP yet, POST an array to `/api/spans` using the harness fields `trace_id`, `span_id`, `parent_span_id`, `name`, `start_unix_nano`, `end_unix_nano`, `status`, `attributes`, and `events`. Wrap it in `{"service":"my-agent","spans":[...]}` to label the producer. Repeated `(trace_id, span_id)` pairs update the stored span.

Report live progress separately:

```bash
curl -X POST http://127.0.0.1:4318/api/progress \
  -H 'Content-Type: application/json' \
  -d '{"trace_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","stage":"retrieving","message":"Found 2 documents","percent":50,"session_id":"conversation-42"}'
```

Read API: `GET /api/metrics` (derived run totals), `GET /api/usage-series?days=7&step=day&service=...` (token, cost, and error trends), `GET /api/measurements` (native OTLP points, filterable by instrument identity), `GET /api/metric-catalog` (available metric instruments), `GET /api/logs`, `GET /api/findings?limit=100&cursor=...` (cursor-paginated review queue), `GET /api/outcomes`, `GET /api/sessions` (per-session trace, token, cost, and status totals), `GET /api/endpoints` (observed host inventory), `GET /api/traces?service=...&status=ERROR&session_id=...&endpoint_id=...&q=model-name&limit=100`, `GET /api/traces/<trace_id>`, `GET /api/traces/<trace_id>/spans?limit=200&q=...&filter=...&cursor=...`, and `GET /api/traces/<trace_id>/related`. Trace search matches trace IDs, workflow/task and span names, service names, sessions, model and tool names, provider and operation names, exception types, correlated log event/severity names, and structured agent activity domains, actions, subjects, and actors. Trace list responses include `next_cursor`; pass it back with the same filters to load the next page. The dashboard exposes this as “Load older traces” and can filter by session or observed endpoint, or search across traces. The span timeline loads cursor pages of 200 spans; search and error/model/tool/agent filters run against stored spans on the server, so opening a large trace does not send every span to the browser. The selected session also shows run outcomes, input/output tokens, known cost and any unknown-cost runs, plus its services and models. Its Model interactions panel shows per-call token counts, latency, known cost, opt-in captured input/output fields, and links each call to its source span. It groups traces only when the producer supplies a shared `gen_ai.conversation.id`, `session.id`, `agent.session_id`, `session_id`, `conversation_id`, `thread_id`, `langgraph.thread_id`, or `langchain.session_id`. See the [OpenTelemetry session conventions](https://opentelemetry.io/docs/specs/semconv/general/session/) and [GenAI agent span conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md). A per-trace activity index and persisted run summary keep listing and metric totals away from repeated trace detail reads. A persisted findings index updates when a trace receives spans, correlated error logs, progress, or an evaluation outcome; existing databases backfill the derived records once. Findings report recorded span/tool errors, incomplete harness runs, repeated identical tool failures, blocked agent activity, error-severity logs, failed task outcomes, and possible credential material in captured span attributes, events, log content, or evaluation outcomes. Credential findings contain source links and field/provider labels but omit matched values; recognizable credentials are also redacted from finding titles and details. The original telemetry remains available in the trace detail view. Each finding names its source span, log, or outcome; these are observations, not causal diagnoses. The related endpoint resolves incoming and outgoing OTLP span links; the dashboard lets you open a linked trace when it has been ingested. The dashboard refreshes every five seconds. The receiver stores raw telemetry immediately and coalesces summary/finding index refreshes per trace with a one-second maximum coalescing window. Progress-only runs appear as `RUNNING`; a harness run stopped by a step budget appears as `INCOMPLETE`. A recorded tool error is visible under `ERROR` even if its OpenTelemetry span status was unset.

By default the server binds only to loopback and the SQLite file is set to user-only permissions. For a shared deployment, set `OBSERVATORY_TOKEN` in the server environment and send `Authorization: Bearer VALUE` on API requests. The browser dashboard asks for the token and keeps it in session storage. Put the service behind TLS and a trusted reverse proxy. Trace attributes can contain prompts, tool outputs, or secrets, so choose what the producer emits. This local receiver does not redact or enforce multiuser access control.

## Verify

```bash
python3 -m unittest discover -s tests -v
```

## Current boundaries

This is a working local vertical slice, not an Origin equivalent. Automatic capture covers the supported Python SDKs in scripts and modules launched through Observatory; existing processes need to restart through the launcher. Other runtimes can send OTLP or use the clients. Managed endpoint collector deployment remains manual, and endpoint inventory depends on host identity resource attributes. It also does not provide team access controls. Session grouping depends on IDs emitted by the producer. Retention is optional and local; it needs operational validation under sustained ingestion. Findings use a small deterministic ruleset and need broader validation before operational use. The dashboard's run counters are derived from stored spans; native metric points are shown separately without unsafe aggregation across metric kinds or temporalities. Progress can come from an explicit API event or a correlated `agent.progress` OTLP log. The protobuf decoder supports the OTLP fields displayed or stored here and skips unknown fields; it is not a general protobuf decoder. Metric exemplars link measurements back to their source trace and span when those IDs are present; histogram bucket charts show one latest distribution point for the selected instrument, with its source attributes shown.

## Workspace integration check

The offline LangGraph retrieval control was run and imported alongside the harness demo. The resulting database contained 3 runs and 23 spans across `agent-harness` and `langgraph`; joining the control's `results.json` correctly marked its failed baseline `FAILED` and its passing control `OK`. This verifies ingestion and outcome correlation for those two local producers, not automatic instrumentation of arbitrary applications.

## Agent session overview

The terminal's **Agents** view lists agent, session ID, repo/project, reported status,
last update, waiting reason, and current/last task. Select a row for the full session
ID, working directory, branch (when recorded), model, usage, and recent work.
Press **Enter** to inspect; **e** expands the details and **s** opens session activity.
Long values remain available in the selectable, copyable detail pane.

Status comes from native lifecycle records: **Working**, **Waiting**, **Completed**,
or **Interrupted**. A known input request shows **User input**; a pending tool shows
**Tool response** and its name. After 35 seconds without new evidence, an unfinished
session shows **No updates**. That does not prove the process stopped or that an
approval is required. Unrecognized/missing lifecycle records remain **Unknown**.
SDK connections report connection state separately from native session state.

The **MCP Calls** view includes an observed server inventory for the entire stored
scope. Select a server and press **Enter** to search its calls. Counters include older
calls beyond the current page; the table's count describes the displayed page.
Configured servers without observed calls are not a connectivity inventory.
Claude Code and Codex qualified names (`mcp__server__tool`) and Codex explicit MCP
completion records carry server/tool identity. Restart the receiver after upgrading
to re-read discoverable local session files; the existing 24-hour discovery window
and bounded file reading limits still apply.

## Optional AI summaries

AI summaries are off until you configure a provider. In **Agents**, select a session
and click **AI summary**. This sends its task, project, reported state, waiting reason,
and up to 30 recent action headlines to your chosen provider. It uses your API quota.
Raw tool payloads, working directories, hidden reasoning, and environment values are
excluded. Recognizable secrets are redacted, but task/headline text may still contain
private project information. Only enable this with a provider appropriate for that data.

Set these in the terminal that launches Observatory:

```sh
export OBSERVATORY_SUMMARY_URL='https://api.openai.com/v1/chat/completions'
export OBSERVATORY_SUMMARY_MODEL='YOUR_MODEL_ID'
export OBSERVATORY_SUMMARY_KEY_ENV='OPENAI_API_KEY'
# OPENAI_API_KEY must already be set securely in this terminal.
agent-observatory
```

The URL is the full **Chat Completions** endpoint, and the model must be available to
your account. Other compatible providers can use their endpoint, model, and API key
variable. The default key variable is `OBSERVATORY_SUMMARY_API_KEY`. A compatible local
server can use an HTTP localhost URL without an API key. No key is saved to the database.
The request uses `max_completion_tokens` and `store: false`; compatible providers must
support those fields. See the [API request schema](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).

Summaries run only on request, in the background. An unchanged snapshot reuses its
summary for the lifetime of the UI; new session activity marks it as an earlier snapshot.
Provider failures leave the dashboard available. Generated text is labeled and does
not override recorded status. Internal approval reviews are excluded.
