<p align="center">
  <img src="media/brand/icon.svg" alt="Agenturm: a Berlin-inspired tower with a purple observation sphere and magnifying glass" width="160" height="160">
</p>

<h1 align="center">Agenturm</h1>

<p align="center">
  <strong>A clear view of every agent.</strong><br>
  A local terminal monitor for Codex, Claude Code, OpenCode, and OpenTelemetry agents.
</p>

<p align="center">
  <a href="https://github.com/abhishekash/agenturm/actions/workflows/ci.yml"><img src="https://github.com/abhishekash/agenturm/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <img src="https://img.shields.io/badge/interface-terminal-ffbd69?labelColor=191621" alt="Terminal interface">
  <img src="https://img.shields.io/badge/data-local-c5adff?labelColor=191621" alt="Data stays local">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-b9afc5?labelColor=191621" alt="MIT license"></a>
</p>

<p align="center">
  <a href="#start">Start</a> · <a href="#see-the-work">See the work</a> · <a href="#go-deeper">Go deeper</a> · <a href="docs/usage.md">Documentation</a>
</p>

## See the work

<p align="center">
  <img src="media/agenturm-terminal.png" alt="Agenturm terminal with its purple tower-and-lens mark, agent sessions, tasks, and recorded progress." width="100%">
</p>

<p align="center">
  <a href="docs/investigation-video.md">Walkthrough and reproduction</a><br>
  <sub>Actual terminal UI. Synthetic example records. No personal prompts or session logs.</sub>
</p>

Several agents, several terminals, and one question: **what has happened so far?**

Agenturm brings the sessions together. See which agents are working, what they
last did, and which ones need your attention. Select a session to read its
recorded progress, then follow the prompts, tool calls, and execution details.

## Start

Requires **Python 3.11+**, Git, and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Run the renamed app directly from its source repository:

```bash
uvx --from git+https://github.com/abhishekash/agenturm agenturm
```

Or install the command once:

```bash
uv tool install --force git+https://github.com/abhishekash/agenturm
agenturm
```

Open it wherever you work. Supported local Codex, Claude Code, and OpenCode
session records are discovered automatically. **No browser, account, or API key
is needed for local monitoring and recorded-progress summaries.**

Already have the checkout? Run `./scripts/install.sh` on macOS/Linux or
`.\scripts\install.ps1` on Windows, then launch `agenturm`.

## One overview. Detail when you need it.

| Start with… | Then inspect… |
| --- | --- |
| **Agents** — sessions, projects, status, last update | A session's goal, recorded progress, current work, and reported waits |
| **Activity** — what happened in the selected session | Your prompts, assistant messages, shell commands, file changes, and errors |
| **MCP calls** — which servers and tools were used | Readable arguments, responses, failures, duration, and the linked execution |
| **Traces** — how a run fits together | Parent/child operations, model usage, and errors |
| **Metrics and host** — resource and usage details | Recorded OTLP metrics and known local processes |

Start agents and answer their requests in their own terminals. Agenturm monitors
the evidence they produce; it shows when a session needs a reply without
pretending it can approve on the agent's behalf.

### Keyboard first

| Key | Action |
| --- | --- |
| `1`–`7` | Switch views |
| `↑` / `↓`, `Enter` | Select and inspect a row |
| `/` | Search the current view |
| `e` | Expand the selected details |
| `t` | Follow the linked trace |
| `Esc` / `Alt+←` | Return / navigate back |
| `c` | Copy details |
| `q` | Quit |

### Four themes

Press **Ctrl+P**, choose **Theme**, then select **Current** (Alpine Ember),
**Bright**, **Dark**, or **Solarized**. Each palette applies to the panels,
tables, controls, and evidence inspectors; Current is the default. The tower-and-lens
mark adapts to each theme, including light backgrounds.

In **Activity**, use **Prompts** to read the prompt sequence for the selected
agent. Search, session scope, and selection help keep other sessions out of the
investigation. Compact terminals can expand details with `e`.

## Local by default

Session records and telemetry stay in a local SQLite database. The built-in
receiver listens on `127.0.0.1:14318`. Recognizable credentials are redacted on a
best-effort basis; `--metadata-only` omits native session text, commands, and
outputs. Read the [capture and privacy boundaries](docs/privacy.md) for details.

Recorded-progress summaries work immediately. Optional AI summaries reuse an
existing `OPENAI_API_KEY` when available and run only when requested. Provider
calls send the documented session evidence to that provider; review
[AI summary setup and data](docs/usage.md#optional-ai-summaries) before enabling
them.

## Go deeper

- [Complete usage guide](docs/usage.md)
- [Python integrations and automatic SDK capture](docs/python.md)
- [Pydantic AI, Google ADK, Gemini CLI, and other OTLP agents](docs/frameworks.md)
- [JavaScript client](docs/javascript.md)
- [Receiver and OTLP configuration](docs/receiver.md)
- [Brand assets and Alpine Ember theme](docs/branding.md)

For querying recorded MCP executions conversationally, see
[SpanHound](https://github.com/abhishekash/spanhound), the companion MCP tool.

## Develop

```bash
git clone https://github.com/abhishekash/agenturm.git
cd agenturm
uv sync
uv run agenturm
uv run python -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and demo production.
[Issues](https://github.com/abhishekash/agenturm/issues) and small pull
requests with clear reproductions are welcome.

<details>
<summary>Upgrading from Agent Observatory</summary>

Agenturm is the new name. Install from this repository to get the new
command. Existing Python imports (`observatory`), `OBSERVATORY_*` settings,
telemetry keys, and `observatory.db` history continue to work. The old command
is also retained as an alias. The renamed PyPI distribution will be available
after its first release; the source install above works independently of that
publication.

</details>

---

**Agenturm** is open source under the [MIT license](LICENSE).
The Berlin-inspired tower-and-lens mark and the terminal's Alpine Ember palette
are included in the same repository.
