# Connect agent frameworks

Agent Observatory combines local session records with telemetry emitted by an
agent runtime. The collection path determines what can be shown; it does not
guess actions that the runtime never recorded.

## Support paths

| Path | Runtimes | Captures |
|---|---|---|
| Local session discovery | Codex CLI and Claude Code | Session/task state and the events present in their local records. Observatory does not attach to arbitrary processes. |
| Local database discovery | OpenCode | Read-only recent session metadata, assistant-turn model/token spans, recorded tool parts, and progress from its persisted task checklist. Reasoning parts are excluded; task text and tool payloads follow the local content-capture setting. |
| Python auto capture | OpenAI Python SDK, Anthropic Python SDK, and LangChain SDKs launched through `agent-observatory run` | OpenInference model and framework spans; content is omitted by default. |
| Python adapters | LangChain callback handler and OpenAI Agents SDK exporter | Framework spans, model usage when supplied, tool activity, errors, and optional progress/content according to each adapter. |
| OTLP | Any language or agent able to export OTLP/HTTP traces, logs, or metrics | The fields the producer emits. GenAI semantic attributes are normalized into model, token, tool, MCP, and session details when present. |
| Gemini CLI | Built-in OpenTelemetry export over OTLP/HTTP | Traces, logs, metrics, token usage, run lifecycle, file/tool activity, and MCP calls; session rows are grouped from `session.id` telemetry without wrapper code or private log scraping. |
| GitHub Copilot CLI | Built-in OpenTelemetry export over OTLP/HTTP | Agent, model, and tool spans; session and token usage; hook, skill, and conversation lifecycle events when emitted. No wrapper required. |
| Cursor Enterprise | Team-managed OpenTelemetry Export over OTLP/HTTP protobuf | Conversation sessions, API request/error activity and token counts, skill/hook/plugin events, cloud-agent events, and recorded Grok Bot MCP calls; Cursor does not export traces, and aggregate metrics do not carry session IDs. |
| Manual clients | Python and JavaScript | Explicit spans and progress; attach the attributes available from the agent. |

The receiver has been exercised with the OpenTelemetry SDK's real OTLP
exporters for traces, logs, and metrics. Framework specific integrations have
their own availability and behavior; the guide does not imply that every
framework automatically captures the same fields.

The dependency-free Node.js client is also exercised end to end against a real
local receiver. The test checks nested parent-child spans, session grouping,
model token attributes, progress events, MCP call identity, resource attributes,
and error capture without sending data to an external service.

CI also runs offline framework workflows through local receivers for Google
ADK with a stdio MCP tool, OpenAI Agents SDK with a scripted tool run, and
LangChain with a fake chat model and tool. These checks exercise the framework
adapters without model API credentials or external inference calls.

The automatic-capture launcher is exercised separately through the real CLI
using mocked OpenAI and Anthropic HTTP transports, including an OpenAI call
inside a LangChain runnable, streaming usage, an expected provider error, and
connection shutdown. The test sends no model requests over the network.

## Start a receiver

The default terminal cockpit starts its receiver on port `14318`:

```bash
agent-observatory
```

Alternatively, run the OTLP receiver directly. `serve` defaults
to port `4318`:

```bash
agent-observatory serve
```

Use the port for the receiver you started. Keep it on loopback for local use.

## Pydantic AI

Pydantic AI's `Instrumentation` capability emits OpenTelemetry spans for agent
runs, model requests, and tool executions. Configure a standard OTLP exporter
before creating the agent:

```bash
pip install 'pydantic-ai' 'opentelemetry-sdk' 'opentelemetry-exporter-otlp-proto-http'
```

```python
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic_ai import Agent
from pydantic_ai.capabilities import Instrumentation

provider = TracerProvider(resource=Resource.create({
    "service.name": "pydantic-ai-agent",
}))
processor = BatchSpanProcessor(OTLPSpanExporter(
    endpoint="http://127.0.0.1:14318/v1/traces",
))
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)

agent = Agent("openai:gpt-4.1-mini", capabilities=[Instrumentation()])
try:
    result = agent.run_sync("Find the latest release in the project notes")
finally:
    provider.force_flush()
    provider.shutdown()
```

If the application already configures a global `TracerProvider`, add the
Observatory `BatchSpanProcessor` to that provider instead of replacing it.
Pydantic AI controls which span attributes it emits; Observatory only displays
the telemetry it receives.

CI runs an offline Pydantic AI agent through a fake model and a real local OTLP
receiver. It checks that the agent, model, and tool spans arrive with usage and
session data; this test needs no provider API key or network model call.

## Google Agent Development Kit

Google ADK Python v1.17.0 and later supports OTLP trace export. For the default
cockpit receiver, configure the trace endpoint before starting `adk web` or
`adk api_server`:

```bash
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:14318/v1/traces
export OTEL_SERVICE_NAME=google-adk-agent
adk web ./agents
```

For programmatic setup:

```python
import os

from google.adk.telemetry.setup import maybe_set_otel_providers

os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = (
    "http://127.0.0.1:14318/v1/traces"
)
os.environ["OTEL_SERVICE_NAME"] = "google-adk-agent"
maybe_set_otel_providers()
```

ADK trace export supplies spans. Configure its logs or metrics exporters
separately if you want those signals in Observatory too.

CI runs an ADK `InMemoryRunner` with a local fake model and a real stdio MCP
fixture against the receiver. It verifies invocation/model/tool spans, session
grouping, token totals, and that MCP initialization and tool discovery are not
counted as tool calls. ADK emits token usage on both a wrapper span and its
nested model span; Observatory counts the nested model calls once in run totals
while retaining every span in trace details. ADK's MCP spans currently identify
the called tool but omit the configured MCP server name, so Observatory shows
`Not reported` for that server unless another emitted span or log supplies it.

## OpenCode local sessions

On a loopback receiver, local discovery reads OpenCode's SQLite database in
read-only mode. It uses the usual
`~/.local/share/opencode/opencode.db` path, honors `XDG_DATA_HOME` and
`OPENCODE_DB`, and never invokes OpenCode or edits its database. Recent session
titles, project names, assistant-turn model/token usage, and tool-call parts are
normalized into the session list and trace view. Reasoning parts are excluded.
The session label uses the database's last-update time; this is activity
recency, not proof that a process is currently running. No approval/user-input
wait is inferred when OpenCode has not recorded one in a supported field.
When OpenCode has persisted a todo checklist, Observatory shows its completed,
in-progress, and pending steps as trace progress with a completion percentage.
Tool inputs and results are captured only when native content capture is
enabled; `--metadata-only` retains tool identity and status but omits payloads
and task text. OpenCode changes its local schema over time, so unrecognized
schema versions may expose fewer fields; OTLP remains the portable path.

## Any OTLP exporter

For SDKs that follow the standard OTLP environment variables and export all
three signals, point them at the receiver:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:14318
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_SERVICE_NAME=my-agent
```

For a standalone `agent-observatory serve` using its default port, replace
`14318` with `4318`. OTLP/HTTP uses `/v1/traces`, `/v1/logs`, and `/v1/metrics`;
the general endpoint above lets standard exporters append those paths.

Useful attributes include:

- `gen_ai.operation.name`, `gen_ai.agent.name`, `gen_ai.request.model`
- `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`
- `gen_ai.conversation.id` or `session.id` for session grouping
- `gen_ai.tool.name`, `mcp.server.name`, `mcp.tool.name`, and `rpc.system=mcp`
- `agent.progress.stage`, `agent.progress.message`, and optional
  `agent.progress.percent` on a correlated `agent.progress` log record

Unknown fields stay unknown. Observatory does not infer token counts, progress,
MCP server identity, or cost when the producer omits them.
An explicitly reported token count of `0` remains a real zero; absent input or
output token attributes are marked **Not reported** in trace and session totals.

## OpenLLMetry Python MCP client

OpenLLMetry's MCP instrumentor emits client spans for tool calls and other MCP
methods. Install it in the agent's environment and enable it after configuring
the OpenTelemetry SDK/exporter described above:

```bash
python -m pip install opentelemetry-instrumentation-mcp
```

```python
from opentelemetry.instrumentation.mcp import McpInstrumentor

McpInstrumentor().instrument()
```

The receiver maps its MCP tool name, operation, input/output fields, and error
status into the MCP Calls view. That instrumentor does not report the configured
server name on these client spans, so the server is shown as **Not reported**.
Its instrumentation records content by default; set
`TRACELOOP_TRACE_CONTENT=false` before starting the agent to keep tool arguments
and results out of telemetry. See the
[instrumentor's privacy and setup notes](https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-mcp).

## Gemini CLI

Gemini CLI has built-in OpenTelemetry export. Start the Observatory cockpit, then
configure `.gemini/settings.json` in your project to send OTLP/HTTP to its default
local receiver port:

```json
{
  "telemetry": {
    "enabled": true,
    "target": "local",
    "otlpProtocol": "http",
    "otlpEndpoint": "http://127.0.0.1:14318",
    "logPrompts": false
  }
}
```

Run `gemini` in the project. When telemetry includes `session.id`, it appears in
the **Agents** view with its latest reported task, model, status, and project name
when the producer sends a repository resource attribute. Open **Activity**,
**MCP Calls**, **Traces**, and **Metrics** to inspect the emitted data. Gemini's documented
`gemini_cli.tool_call` events are normalized into the shared activity/MCP schema,
including server, tool, result, duration, and session when provided. Gemini CLI
can include detailed prompt and tool content when trace detail capture is enabled;
review its privacy settings before opting into detailed traces. The CLI documents
local OTLP export and all three telemetry signals in its
[OpenTelemetry configuration guide](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/telemetry.md).

An interoperability fixture checks the documented Gemini MCP log shape through
the same receiver normalization and stored MCP/activity queries used by the UI.

## Cursor Enterprise

Cursor's Enterprise OpenTelemetry Export is configured by a team admin under
**Team Settings → OpenTelemetry Export**. It sends OTLP/HTTP protobuf metrics
and logs to an HTTPS endpoint reachable by Cursor's servers. The default local
Observatory receiver is loopback-only and has no TLS or authentication, so it
cannot be used as Cursor's direct destination. Put an authenticated HTTPS
collector or gateway in front of the receiver and keep its network access
restricted; do not expose the local receiver directly to the internet.

Set the destination base URL (without `/v1`), configure the auth header, and
enable the desired signal families. Cursor appends `/v1/metrics` and `/v1/logs`.
The team must have Cursor Enterprise. In Observatory, session rows use
`cursor.conversation.id`; `cursor.api.request` events supply model and token
counts, and `cursor.grok_bot.mcp_tool_call` appears in Activity and MCP Calls
when Cursor Action Recording is enabled. Cached read and cache creation tokens
are included in the session and terminal overview input totals; their original Cursor
fields remain visible in event metadata. `cursor.tool.calls` MCP metrics appear
in the MCP server inventory as aggregate counts, separate from individual calls;
they cannot be assigned to a conversation. Skill, hook, plugin, and cloud-agent
logs also appear in Activity.

Cursor does not export traces or span context, so these sessions have activity
and usage without fabricated traces. Its token/tool/cost metrics are delta
aggregates with no session IDs; they appear in Metrics and cannot be assigned
to individual conversations. Conversation messages and MCP arguments/results
are off by default in Cursor. Observatory displays them only if the team
explicitly enables Cursor's `conversation_content` family and emits those logs;
review Cursor's [privacy and wire-format documentation](https://prod.cursor.com/docs/enterprise/opentelemetry-export)
before enabling that family. Log retries are deduplicated using
`cursor.event.id`.

## Further setup

- [Python auto capture and SDK adapters](python.md)
- [OTLP receiver, Collector relay, and signal details](receiver.md)
- [Full user guide and telemetry field mapping](usage.md)
