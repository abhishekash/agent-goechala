## 0.1.2 · Native session status, MCP inventory, optional AI summary

Shows agent and session IDs, repo/project, lifecycle status, update age, pending wait, and task details. MCP server calls now link across start/result events, expose qualified Claude Code and Codex tool names, report completion/error status, and include all stored history in server counts. Optional provider-backed session summaries run only when requested. DuckDB guidance is in [the storage evaluation](decisions/duckdb.md).

# Agent Goechala

A terminal-first monitor and local telemetry receiver for AI agents. It discovers supported local agent sessions, then accepts **OTLP/HTTP traces, logs, and metrics in protobuf or JSON** from instrumented runtimes and the existing **agent-harness JSONL** format. The terminal cockpit shows agents, activity, MCP calls, traces, host processes, and metrics. Data lives in SQLite. The receiver uses Python's standard library (3.11+); the terminal UI uses Textual.

## GitHub Copilot CLI telemetry

Copilot CLI can send its built-in OpenTelemetry traces and metrics directly to
the cockpit receiver. Start `agent-goechala` in one terminal, then launch
Copilot CLI from the project in another:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:14318
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_SERVICE_NAME=github-copilot
copilot
```

Setting the endpoint enables Copilot telemetry. The CLI defaults to
`http/json`; the protocol is shown explicitly here. The cockpit groups spans
into sessions when Copilot supplies `gen_ai.conversation.id` and reports model
usage, tool activity, and recognized hook, skill, compaction, truncation, and
session lifecycle events. Tool records contain only fields the CLI exports;
the MCP Calls view can identify a server or tool only when those identities are
present in telemetry. Prompt and response capture is off by default in Copilot
CLI; keep it disabled unless you intend to collect that content. Copilot's
[OTel settings and span/event schema](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference#opentelemetry-monitoring)
describe the emitted signals and optional content capture.

## Install and launch the terminal cockpit

Python 3.11+ is required. Install the CLI in an isolated environment with [uv tools](https://docs.astral.sh/uv/guides/tools/):

```bash
uv tool install --force git+https://github.com/abhishekash/agent-goechala
agent-goechala
```

If uv is not installed, use its [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/). If the installed command is missing from your PATH, run `uv tool update-shell` and open a new terminal. You can also install into an active Python environment with `python -m pip install "agent-goechala @ git+https://github.com/abhishekash/agent-goechala"`.

The cockpit opens a tabbed interface for Agents, Activity, MCP Calls, Traces, Host, Metrics, and Findings. The Agents view shows individual sessions, tasks, models, recent work, and an immediate recorded-progress summary. Findings provides a searchable, paged review queue with occurrence counts, evidence links, and direct navigation to the source trace. Session counts and live process counts are separate; approval reviews are hidden by default. The cockpit attaches to a receiver on **127.0.0.1:14318**, or starts one there. Run `agent-goechala` for full-screen panels in your current terminal. From this checkout, `python3 -m observatory` also works without installation.

### Existing Codex and Claude Code agents

Local receivers automatically discover recent session files under `~/.codex/sessions` and `~/.claude/projects` (`CODEX_HOME` and `CLAUDE_CONFIG_DIR` are respected). Start the monitor with no agent filename or Python environment flags:

```bash
agent-goechala
```

It reads actual logged activity as files update, including already running sessions. Agents need no restart or configuration change. Source logs stay untouched. Select an agent to see a zero-setup progress summary and its recent activity. When a session explicitly asks for a user reply or approval, the monitor points you to that agent's own terminal; it cannot submit the answer or approve actions for the agent.

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

Discovery scans every 10 seconds and tails discovered files every second. It selects up to 50 Codex/Claude Code files modified within the past 24 hours and up to 50 OpenCode sessions in that window, importing at most the last 8 MiB initially per file. If a limit is reached, the terminal health line reports how many sessions are outside the visible window. Summaries may cover only part of a long session. Codex response IDs and Claude message IDs prevent duplicate usage entries on reread; older Codex cumulative token snapshots are handled separately. A version with a different log schema may expose less activity. `GET /api/health` reports discovery status, session/task snapshots, runtime metadata, source count, read errors, and skipped malformed records. `GET /api/activity?limit=100&service=codex&domain=shell&q=...` exposes a bounded, searchable activity view from the latest 100 observed traces. The terminal shows up to 100 recent events, with full trace investigation available separately.

Local discovery runs in the receiver. If you installed an earlier version, reinstall the CLI and restart that receiver to enable it. A cockpit attached to an older receiver cannot enable discovery there. Add `--no-local-agents` to `serve` or `cockpit` to disable discovery in a receiver started by that command. Receivers bound to non-loopback addresses do not discover local sessions automatically. Other agent products still need an adapter or OTLP export.

### OpenCode local database

On loopback, the receiver checks OpenCode's local SQLite database in read-only
mode and lists recent sessions with their project, task title, model, and
assistant-turn token counts. Recorded tool parts appear as trace spans; persisted
todo checklists appear as progress events with completion percentages. MCP names
are attributed when OpenCode records an explicit qualified server/tool name.
The usual database path is `~/.local/share/opencode/opencode.db`; `XDG_DATA_HOME`
and `OPENCODE_DB` are honored. Reasoning parts are never read. Inputs and results
follow the same bounded, redacted content policy as other native sources; pass
`--metadata-only` to omit those payloads and task text. Discovery covers the 50
most recently updated sessions from the last 24 hours, up to 30 assistant
messages per session, 200 tool parts per message, and 100 checklist items per
session. OpenCode's internal schema
can change; use OTLP for a stable cross-version integration. Its session label
is based on database update recency, not proof of a live process; Goechala
does not infer a waiting-for-approval state from inactivity.

### Optional Python SDK capture

Native session discovery is automatic for supported local agents. For another Python agent, automatic SDK capture is a separate command-line workflow; the monitoring cockpit itself does not launch agents. Install the capture extra into the agent's environment:

```bash
uv pip install --python .venv/bin/python 'agent-goechala[auto] @ git+https://github.com/abhishekash/agent-goechala'
agent-goechala run --endpoint http://127.0.0.1:14318 --service my-agent agent.py
```

Replace the environment path, service name, and agent filename with your own. Arguments after `agent.py` go to the script; `--module` runs a Python module. If Goechala is already installed in the active agent environment, use `python -m observatory run --endpoint http://127.0.0.1:14318 --service my-agent agent.py`.

This launcher runs the selected script in that terminal and is intended for SDKs not already captured from local session records. Existing processes can send OTLP telemetry to the receiver. Python SDK prompts and responses are omitted unless you add `--capture-content`. Native session capture has its own `--metadata-only` receiver setting.

Use the mouse or **1–7** to switch views. **↑/↓** selects a table row; its inspector updates automatically. **Enter** opens the inspector; **e** expands it to use the full workspace and **Esc** returns to the list. On compact terminals, Enter expands the inspector automatically. **Tab** moves between controls and scrollable panels. On Agents, **s** focuses the selected session's activity. **t** opens the selected activity, MCP operation, finding, or metric exemplar's related trace when one is linked. **Alt+Left** or the **Back** button returns to the original event and scroll position; **Esc** first clears a text selection or closes an expanded inspector, then returns from a linked trace. **/** focuses the current view's search, **Esc** returns to its table, **a** resets session and search filters, **v** toggles approval reviews, **p** pauses updates, and **q** quits. When search has focus, printable keys enter text; press **Esc** before using shortcuts.

Each view has its own search field and result count directly above its rows. Search in Agents filters sessions by task, project, model, runtime, or session ID. Activity, MCP Calls, and Traces query their own records; Host filters the process list; Metrics filters OTLP instruments by name, service, kind, unit, or description; Findings searches severity, finding type, task, and evidence text in the current page. Findings are global across sessions and can be paged with **Older**, **Newer**, and **Latest**. The Metrics view shows the latest stored point for each instrument with its value, unit, point attributes, resource attributes, and data. Select an instrument to load its latest 100 points; points with OTLP exemplars can open the linked trace with **t** or **Open exemplar**. It loads up to 500 catalog entries and 200 recent points for the overview, refreshing this inventory every three seconds. The session Scope control filters Metrics by the selected session's service; metric points are not attributed to individual sessions unless the telemetry itself includes that association. Filters stay with their view when switching tabs. A task search in Agents does not hide MCP calls or reduce the selected agent's recent-work feed. **Clear** resets that view's query; the session Scope control applies across views. Overview counters retain the inventory for the selected session scope. The **Errors** button in Activity and MCP Calls filters failures within the current results; it does not change the inventory counts. **a** also resets these error filters. Tables resize their columns to the terminal, preserve the selected record, and keep latency and status visible. Inspectors wrap long text to the panel width.

MCP shows reported servers, tools or methods, state, duration, arguments/results and trace/span IDs. It includes protocol methods such as `tools/call`, `tools/list`, `resources/read`, and `prompts/get` when telemetry records them. Calls without a reported server remain visible under **Not reported**; Goechala does not guess their server. Tool execution spans are joined to transport calls to fill in arguments or results when the transport span omits them. Explicit MCP `isError` results mark the run and call as failed, appear in error filters, and create an evidence-linked finding even when the producing span has no error status. The trace view shows a parent-linked span tree; unknown durations remain “not reported”. “Error” describes recorded errors within a trace; “Active · errors” means a logged active turn also contains an error. Agent “Recent/Idle” states describe observed session activity. Live process counts exclude zombie/exited states and do not claim a session-to-PID mapping.

The view polls about once a second and preserves the event being read as new records arrive. Activity, MCP Calls, and Traces have **Older**, **Newer**, and **Latest** controls (shortcuts **]**, **[**, and **l**) to browse saved records in pages of 200 events, 100 MCP calls, or 100 traces. Search and scope changes return that view to its latest matching page. Activity history includes saved logs beyond a trace's preview window. The selected trace initially shows 100 spans; related-trace navigation also loads its exact target span and up to eight missing ancestors. MCP overview counts cover the latest loaded operations in scope. Global token totals include stored history and repeated/cached context; costs remain unreported when telemetry has no price. History controls require the updated receiver; if a separate receiver is already running, restart it after updating.

Drag across inspector text to highlight it, then press **Ctrl+C** (or **Cmd+C** where the terminal forwards it) to copy the selected plain text. Display updates are held while text is selected; click or press **Esc** to clear the selection and resume. **c** copies the selection, or the current event/call/details when nothing is selected. The Activity and MCP inspectors also have copy buttons. Clipboard commands run in the background and use Wayland, X11, macOS, or Windows tools when available, falling back to a terminal clipboard request. When using a remote terminal, clipboard behavior depends on that terminal's support. Narrow terminals stack the list and inspector; **e** expands the inspector for reading.

`GET /api/activity?limit=200&cursor=...&q=...` and `GET /api/mcp?limit=100&cursor=...&service=codex&session_id=...&q=...` return `next_cursor` for older pages. Keep the same filters when following a cursor. The MCP endpoint returns recorded protocol operations with server, tool or method, duration, arguments/results and related trace/span IDs. This includes non-tool methods such as `tools/list`, `resources/read`, and `prompts/get` when provided by telemetry. Linked logs and spans are joined; older unlinked duplicates for the same event are omitted when a linked record exists. Distinct explicit spans remain separate, including concurrent calls sharing a timestamp.

Quitting closes only the cockpit and any receiver it started. Agents and separately started receivers stay running. To keep a receiver running between cockpit sessions, start `python -m observatory serve --port 14318` separately.

```bash
# Connect to an existing receiver without starting one:
agent-goechala cockpit --endpoint http://127.0.0.1:14318
# Print a snapshot for shell use:
agent-goechala cockpit --endpoint http://127.0.0.1:14318 --once
# Reinstall after updating this checkout:
uv tool install --reinstall ./agent-goechala
```

The current terminal is the default. Add `--new-window` only when you want a separate graphical terminal (Terminal on macOS; GNOME Terminal, Konsole, or x-terminal-emulator/xterm on Linux; Windows Terminal on Windows). In SSH or headless sessions, use the default. The Textual interface supports Linux, macOS and Windows terminals without a curses dependency.

## Capture an agent without changing its code

For Python agents that use supported model SDKs, install the automatic capture extra and run `agent-goechala run` from the agent's own environment. This is a separate terminal command; the monitoring cockpit remains read-only and does not start or control an agent.

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

The replay uses the current SDKs' `httpx2` mock transport and records an OpenAI call inside a LangChain runnable, an async OpenAI stream, an Anthropic message, and an expected error. All model responses are local replay data. Telemetry goes only to your selected Goechala receiver.

## Run

```bash
cd agent-goechala
python3 -m observatory --db ./observatory.db serve
# The root address is a plain-text receiver status page; use the terminal cockpit to inspect activity.
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

To watch a live harness run, pass its trace file to the server. New finished spans appear in the terminal cockpit within about five seconds:

```bash
python3 -m observatory --db ./observatory.db serve --watch-jsonl /path/to/live-trace.jsonl
```

For a shared local relay, start the [OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib) distribution with `otelcol-contrib --config ./examples/otel-collector-config.yaml`. This sample accepts OTLP/gRPC on `127.0.0.1:4317` and OTLP/HTTP on `127.0.0.1:4319`, detects host name, architecture, and OS attributes, then batches traces, logs, and metrics to Goechala on port 4318. Point SDKs at the relay with `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317` and `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`, or use the HTTP listener with `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4319` and `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`. The sample binds to loopback; change the receiver endpoints only when the network path is protected.

Send OTLP/HTTP traces from another agent runtime to `http://127.0.0.1:4318/v1/traces`, logs to `/v1/logs`, and metrics to `/v1/metrics`. Standard HTTP/protobuf exporters use `Content-Type: application/x-protobuf`; HTTP/JSON exporters use `application/json`. Gzip request bodies are accepted. For a typical SDK, set `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318` and `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`. The receiver keeps trace parent IDs, attributes, events, links, resource attributes, instrumentation scope name, and status. Its endpoint inventory groups spans, logs, and metrics by `host.id`, `host.name`, `device.id`, or `service.instance.id` when producers include those resource attributes; it reports OS, architecture, user, environment, services, last seen time, and linked trace counts. Logs with a valid trace ID appear alongside that trace; `agent.progress` log events with `agent.progress.stage` and optional `agent.progress.percent` attributes also appear in its progress list; `event.name` and the equivalent `progress.*` fields are accepted as aliases. Gauge, sum, histogram, exponential histogram, and summary metric points retain their type, timestamp, attributes, unit, and available value or distribution fields, including the sum monotonicity flag that distinguishes counters from up/down sums; no-recorded-value points remain visible as missing readings and are excluded from charts. Cursor MCP aggregate metrics stay separate from recorded calls: delta counts are summed, while cumulative counters use the latest point per attribute/resource series, including after resets. `service.name` identifies each producer. Token counts use `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`, the harness's `llm.usage.*` fields, and OpenInference `llm.token_count.prompt` / `llm.token_count.completion`; OpenInference LLM and embedding model names, indexed chat messages, and embedding input/vector summaries are also recognized. Cache read/write input counts and reasoning output counts are retained as breakdowns when reported; these are subsets of headline totals and are not added a second time. Cost totals accept `llm.cost.total`, the prompt/completion cost components, and the existing harness aliases. Unknown prices are shown as unknown, rather than zero cost. See the [OTLP exporter protocol configuration](https://opentelemetry.io/docs/languages/sdk-configuration/otlp-exporter/) for SDK settings.

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
import { Client } from '@agent-goechala/client';

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
pip install 'agent-goechala[langchain] @ git+https://github.com/abhishekash/agent-goechala'
```

Attach the handler inside an Goechala run and pass it through the runnable config. It records nested chain, model, tool, and retriever spans; captures standardized token usage when the model supplies it; records errors; and maps LangChain custom callbacks named `progress`, `agent.progress`, or `langgraph.progress` into the run timeline. LangGraph applications that use custom stream output can call `run.aprogress(...)` as they consume those events. Prompts, tool inputs, and outputs stay out of telemetry unless `capture_content=True` is set.

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
pip install 'agent-goechala[openai-agents] @ git+https://github.com/abhishekash/agent-goechala'
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

The adapter keeps parent/child relationships and model or token metadata when available, including cache read/write and reasoning usage details. It maps SDK IDs to stable Goechala IDs, maps trace `group_id` to Goechala session grouping, and records original trace/span IDs as attributes for correlation. Prompt, tool input/output, and arbitrary trace metadata are omitted by default. To collect span input/output, opt in with `create_processor(telemetry, capture_content=True)`; content is truncated to 4,000 characters per field by default and can be adjusted with `max_content_chars`. Token totals come from generation spans, avoiding double counting the SDK's aggregate turn usage. The SDK batching processor exports in the background; call `flush_traces()` when a job needs immediate visibility. `set_trace_processors` replaces the SDK's default OpenAI exporter. If you use `add_trace_processor` to keep that exporter too, the SDK also sends its full trace payloads to OpenAI.

For custom runtimes that cannot export OTLP yet, POST an array to `/api/spans` using the harness fields `trace_id`, `span_id`, `parent_span_id`, `name`, `start_unix_nano`, `end_unix_nano`, `status`, `attributes`, and `events`. Wrap it in `{"service":"my-agent","spans":[...]}` to label the producer. Repeated `(trace_id, span_id)` pairs update the stored span.

Report live progress separately:

```bash
curl -X POST http://127.0.0.1:4318/api/progress \
  -H 'Content-Type: application/json' \
  -d '{"trace_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","stage":"retrieving","message":"Found 2 documents","percent":50,"session_id":"conversation-42"}'
```

Read API: `GET /api/metrics` (derived run totals), `GET /api/usage-series?days=7&step=day&service=...&session_id=...` (token trends from traces and log-only sessions, trace cost, and separate log event/error counts; pass `session_id` to narrow the series to one agent session), `GET /api/measurements` (native OTLP points, filterable by instrument identity), `GET /api/metric-catalog` (available metric instruments), `GET /api/logs`, `GET /api/findings?limit=100&cursor=...` (cursor-paginated review queue), `GET /api/outcomes`, `GET /api/sessions` (per-session trace, token, cost, and status totals), `GET /api/endpoints` (observed host inventory), `GET /api/traces?service=...&status=ERROR&session_id=...&endpoint_id=...&q=model-name&limit=100`, `GET /api/traces/<trace_id>`, `GET /api/traces/<trace_id>/spans?limit=200&q=...&filter=...&cursor=...`, and `GET /api/traces/<trace_id>/related`. Trace search matches trace IDs, workflow/task and span names, service names, sessions, model and tool names, provider and operation names, exception types, correlated log event/severity names, and structured agent activity domains, actions, subjects, and actors. Trace list responses include `next_cursor`; pass it back with the same filters to load the next page. The terminal cockpit pages traces, exposes observed run outcomes and usage totals, and links model interactions to their source spans. It groups traces only when the producer supplies a shared `gen_ai.conversation.id`, `session.id`, `agent.session_id`, `session_id`, `conversation_id`, `thread_id`, `langgraph.thread_id`, `langchain.session_id`, or `cursor.conversation.id`. See the [OpenTelemetry session conventions](https://opentelemetry.io/docs/specs/semconv/general/session/) and [GenAI agent span conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md). A per-trace activity index and persisted run summary keep listing and metric totals away from repeated trace detail reads. A persisted findings index updates when a trace receives spans, correlated error logs, progress, or an evaluation outcome; existing databases backfill the derived records once. Findings report recorded span/tool errors, incomplete harness runs, repeated identical tool failures, blocked agent activity, error-severity logs, failed task outcomes, and possible credential material in captured span attributes, events, log content, or evaluation outcomes. Credential findings contain source links and field/provider labels but omit matched values; recognizable credentials are also redacted from finding titles and details. The original telemetry remains available in the trace detail view. Each finding names its source span, log, or outcome; these are observations, not causal diagnoses. The related endpoint resolves incoming and outgoing OTLP span links. The receiver stores raw telemetry immediately and coalesces summary/finding index refreshes per trace with a one-second maximum coalescing window. Progress-only runs appear as `RUNNING`; a harness run stopped by a step budget appears as `INCOMPLETE`. A recorded tool error is visible under `ERROR` even if its OpenTelemetry span status was unset.

Trace and session summaries include `input_tokens_reported` and
`output_tokens_reported` booleans. A false value means the producer did not
send that token attribute; the numeric total remains `0` for aggregation, while
the terminal UI displays **Not reported**. A reported numeric zero remains a
real zero. Summaries also expose `cache_read_input_tokens`,
`cache_write_input_tokens`, and `reasoning_output_tokens`, each with a matching
`*_reported` flag for the breakdown.

By default the server binds only to loopback and the SQLite file is set to user-only permissions. For a shared deployment, set `OBSERVATORY_TOKEN` in the server environment and send `Authorization: Bearer VALUE` on API requests; set the same token in the terminal before starting the cockpit. Put the service behind TLS and a trusted reverse proxy. Trace attributes can contain prompts, tool outputs, or secrets, so choose what the producer emits. This local receiver does not redact or enforce multiuser access control.

## Verify

```bash
python3 -m unittest discover -s tests -v
```

## Current boundaries

This is a working local vertical slice, not an Origin equivalent. Automatic capture covers the supported Python SDKs run through the separate `run` command; existing processes need OTLP export or restart through that launcher. Other runtimes can send OTLP or use the clients. Managed endpoint collector deployment remains manual, and endpoint inventory depends on host identity resource attributes. It also does not provide team access controls. Session grouping depends on IDs emitted by the producer. Retention is optional and local; it needs operational validation under sustained ingestion. Findings use a small deterministic ruleset and need broader validation before operational use. Run counters are derived from stored spans; native metric points are shown separately without unsafe aggregation across metric kinds or temporalities. Progress can come from an explicit API event or a correlated `agent.progress` OTLP log. Summary metrics have no OTLP temporality field; Goechala marks their count and sum as implicitly cumulative. The protobuf decoder supports the OTLP fields displayed or stored here and skips unknown fields; it is not a general protobuf decoder. Metric exemplars link measurements back to their source trace and span when those IDs are present; histogram bucket charts show one latest distribution point for the selected instrument, with its source attributes shown.

## Workspace integration check

The offline LangGraph retrieval control was run and imported alongside the harness demo. The resulting database contained 3 runs and 23 spans across `agent-harness` and `langgraph`; joining the control's `results.json` correctly marked its failed baseline `FAILED` and its passing control `OK`. This verifies ingestion and outcome correlation for those two local producers, not automatic instrumentation of arbitrary applications.

## Agent session overview

The terminal's **Agents** view lists agent, session ID, repo/project, reported status,
last update, waiting reason, and current/last task. Select a row for the full session
ID, working directory, branch (when recorded), model, usage, and recent work.
Press **Enter** to inspect; **e** expands the details and **s** opens session activity.
Long values remain available in the selectable, copyable detail pane.

In **Activity**, select an agent with the Scope control (or press **s** on its row
in Agents), then click **Prompts** beside Errors. This shows only recorded user
messages for that agent, newest first; use **]** to page backward through older
prompts. Turn Prompts off to return to the full activity stream.

Status comes from native lifecycle records: **Working**, **Waiting**, **Completed**,
or **Interrupted**. A known input request shows **User input**; a pending tool shows
**Tool response** and its name. After 35 seconds without new evidence, an unfinished
session shows **No updates**. That does not prove the process stopped or that an
approval is required. Unrecognized/missing lifecycle records remain **Unknown**.
SDK connections report connection state separately from native session state.

The **MCP Calls** view includes configured server names discovered in known local
Codex, Claude Code, Cursor, Gemini CLI, GitHub Copilot CLI, OpenCode, and VS Code configuration files, alongside
servers with calls in the stored scope. Configured-only entries are labeled
**Configured**; this is a name inventory and does not check whether a server is
reachable or running. Only server names, client, and scope are retained or returned;
commands, arguments, URLs, headers, and environment values are discarded. Select a server
and press **Enter** to search its calls. Counters include older calls beyond the
current page; the table's count describes the displayed page. The view reports a
count when a known configuration file is unreadable, malformed, or over the 1 MiB
scan limit; it does not expose the path or contents of that file.
OpenCode project `opencode.json` and `opencode.jsonc` files support both the
top-level `mcp` layout and the nested `mcp.servers` layout. For a running local
OpenCode process, the monitor checks its working directory for project config.
Claude Code and Codex qualified names (`mcp__server__tool`) and Codex explicit MCP
completion records carry server/tool identity. Restart the receiver after upgrading
to re-read discoverable local session files; the existing 24-hour discovery window
and bounded file reading limits still apply.

## Optional AI summaries

Every session first shows a local **Progress summary · recorded activity**. It needs no
provider, key, or extra setup and never guesses the agent's remaining tasks. If
`OPENAI_API_KEY` is already set in the terminal environment, optional AI summaries use
that key with OpenAI's Chat Completions endpoint and `gpt-4.1-mini` by default. In
**Agents**, select a session and click **AI summary** to request one. Otherwise, the
local summary stays available without setup. The request sends its task, project, reported state, waiting reason,
observed trace/span totals, token counts, error counts, cost coverage, and up to 30 recent action headlines to your chosen provider. It uses your API quota.
Unavailable totals are sent as unknown rather than zero. Raw tool payloads, working directories, hidden reasoning, and environment values are
excluded. Recognizable secrets are redacted, but task/headline text may still contain
private project information. Only enable this with a provider appropriate for that data.

For another OpenAI-compatible provider, set these in the terminal that launches Goechala:

```sh
export OBSERVATORY_SUMMARY_URL='https://api.openai.com/v1/chat/completions'
export OBSERVATORY_SUMMARY_MODEL='YOUR_MODEL_ID'
# The existing OPENAI_API_KEY is reused unless OBSERVATORY_SUMMARY_KEY_ENV selects another variable.
agent-goechala
```

The URL is the full **Chat Completions** endpoint, and the model must be available to
your account. Other compatible providers can use their endpoint, model, and API key
variable. The default key variable is `OBSERVATORY_SUMMARY_API_KEY`. A compatible local
server can use an HTTP localhost URL without an API key. `OPENAI_BASE_URL` and
`OPENAI_MODEL` are also respected when reusing `OPENAI_API_KEY`. No key is saved to the database.
The request uses `max_completion_tokens` and `store: false`; compatible providers must
support those fields. See the [API request schema](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).

Summaries run only on request, in the background, with a 12-second timeout. An unchanged
snapshot reuses its summary for the lifetime of the UI; new session activity marks it as
an earlier snapshot. Provider failures leave the local progress summary available.
Generated text is labeled and does
not override recorded status. Internal approval reviews are excluded.
