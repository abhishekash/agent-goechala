# Agent Observatory

[![CI](https://github.com/abhishekash/agent-observatory/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/abhishekash/agent-observatory/actions/workflows/ci.yml)
[![GitHub stars](https://img.shields.io/github/stars/abhishekash/agent-observatory?style=social)](https://github.com/abhishekash/agent-observatory/stargazers)
[![PyPI](https://img.shields.io/pypi/v/agent-observatory-cli)](https://pypi.org/project/agent-observatory-cli/)
[![Downloads](https://img.shields.io/pypi/dm/agent-observatory-cli?label=downloads%2Fmonth)](https://pypistats.org/packages/agent-observatory-cli)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/abhishekash/agent-observatory/blob/main/LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![OpenTelemetry OTLP](https://img.shields.io/badge/OpenTelemetry-OTLP-425CC7?logo=opentelemetry&logoColor=white)
![Local first](https://img.shields.io/badge/data-local--first-2E8B57)

**See what your agents are doing, live.** Agent Observatory watches local Codex and Claude Code sessions, receives OpenTelemetry from instrumented agents, and brings tasks, model calls, MCP tools, traces, tokens, errors, and host activity into one terminal cockpit and local web dashboard.

<p align="center">
  <img src="https://raw.githubusercontent.com/abhishekash/agent-observatory/main/media/terminal-demo.gif" alt="Animated preview of the Agent Observatory terminal cockpit with agents, MCP calls, traces, and host processes" width="960">
</p>

<p align="center"><em>Short demo using synthetic example data. No real session logs or user prompts are included.</em></p>

## Start in one command

Requires Python 3.11+. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if needed, then:

```bash
uvx --from agent-observatory-cli agent-observatory
```

To keep the command installed:

```bash
uv tool install agent-observatory-cli
agent-observatory
```

Or install it into the active Python environment with `python -m pip install agent-observatory-cli`.

The terminal cockpit discovers recent local Codex and Claude Code sessions automatically. Press **b** to open the local web dashboard. It listens on `127.0.0.1:14318`.

### Your first minute

1. Open **Agents** to see sessions, projects, current tasks, and recent work.
2. Open **Activity** or **MCP Calls**, select a row, and press **Enter** to inspect its recorded input and output. Press **e** for a wider view.
3. Press **b** for the browser dashboard, or use **Older / Newer / Latest** to explore stored activity in the terminal.

No sessions yet? Start a local Codex or Claude Code session and perform a task.
Other agent runtimes need [instrumentation or OTLP export](https://github.com/abhishekash/agent-observatory/blob/main/docs/python.md).
Available details depend on what the agent records; unsupported clients and
unrecorded calls do not appear automatically.

Already cloned the repository? Run the installer for your shell:

```bash
./scripts/install.sh
```

```powershell
.\scripts\install.ps1
```

These install the checkout as an isolated uv tool. Re-run the script after pulling an update. No administrator access is needed.

## What you get

- **Agents:** distinct sessions, project, current task, model, recent actions, and activity state.
- **Activity:** readable user/assistant messages, shell commands, file changes, browser events, model use, and tool results.
- **MCP:** server and tool calls with readable arguments and results, an Errors filter, latency, and direct links to the execution span.
- **Traces:** parent/child spans, model usage, errors, and evidence for each operation.
- **Host:** known local agent processes and basic resource use, shown separately from sessions.
- **Web dashboard:** searchable live activity with the same local receiver.

The cockpit adapts from compact terminals to wide workspaces. Search stays with each view, **Enter** inspects a row, **e** expands the details, and **Esc** returns to the list. Drag to highlight inspector text and press **Ctrl+C** to copy it; **c** copies the current details. Activity, MCP Calls, and Traces include **Older / Newer / Latest** controls for stored history, and **Alt+Left** returns from a linked trace to the event you were reading.

The receiver stores data in a local SQLite file. Native session capture is bounded and redacts recognizable credentials on a best-effort basis. Choose metadata-only capture with `--metadata-only`. See [Privacy and capture boundaries](https://github.com/abhishekash/agent-observatory/blob/main/docs/privacy.md).

## Connect other agents

Send OpenTelemetry traces, logs, and metrics over OTLP/HTTP, or use the Python and JavaScript clients:

- [Python setup and automatic SDK capture](https://github.com/abhishekash/agent-observatory/blob/main/docs/python.md)
- [Node.js client](https://github.com/abhishekash/agent-observatory/blob/main/docs/javascript.md)
- [OTLP, receiver, and deployment configuration](https://github.com/abhishekash/agent-observatory/blob/main/docs/receiver.md)
- [Complete usage guide and examples](https://github.com/abhishekash/agent-observatory/blob/main/docs/usage.md)

For MCP clients that need to query saved traces conversationally, see
[SpanHound](https://github.com/abhishekash/spanhound), a local MCP
server for trace failures, approvals, token usage, and run comparisons.
SpanHound reads JSONL and OTLP JSON files and can capture supported Python library calls.

## Help improve agent observability

[Tell us about your first run](https://github.com/abhishekash/agent-observatory/issues/new?template=first_run.yml):
which agent you use, what you expected to see, and what was missing or confusing.
Share a redacted example when reporting a capture or formatting problem.

## Develop

```bash
git clone https://github.com/abhishekash/agent-observatory.git
cd agent-observatory
uv tool install --editable .
python -m unittest discover -s tests -v
```

Contributions and bug reports are welcome. See [CONTRIBUTING.md](https://github.com/abhishekash/agent-observatory/blob/main/CONTRIBUTING.md) and [open an issue](https://github.com/abhishekash/agent-observatory/issues).

Maintainers can follow the [PyPI publishing guide](https://github.com/abhishekash/agent-observatory/blob/main/docs/publishing.md) to release a version with Trusted Publishing.

## License

Agent Observatory is distributed under the [MIT License](https://github.com/abhishekash/agent-observatory/blob/main/LICENSE).
