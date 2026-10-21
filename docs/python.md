# Python agents

## Install the CLI

Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/) are required. Install the published CLI:

```bash
uv tool install --force git+https://github.com/abhishekash/agenturm
agenturm
```

For an existing checkout, run `./scripts/install.sh` (or `scripts/install.ps1` on Windows).

## Observe local Codex and Claude Code sessions

Start `agenturm`. Recent local sessions are discovered automatically on the same machine. The monitor runs in the terminal and does not start agents.

## Automatically instrument a Python SDK agent

Install the automatic capture extra into the same environment as your agent:

```bash
uv pip install --python .venv/bin/python 'agenturm[auto] @ git+https://github.com/abhishekash/agenturm'
```

Run your script with the separate capture command:

```bash
.venv/bin/python -m observatory run --endpoint http://127.0.0.1:14318 --service research-agent agent.py
```

Replace the service name, Python executable, and script with yours. Use `--module` before the script for a Python module. Script arguments follow the script name. This separate launcher captures supported OpenAI, Anthropic, and LangChain SDK calls. Prompts and responses are excluded unless `--capture-content` is given.

## Add manual spans

Install the client into the agent environment, then use `Client`:

```python
from observatory import Client

telemetry = Client(service="research-agent")
with telemetry.trace(task="Research a question", session_id="thread-42") as run:
    with run.span("search", attributes={"tool.name": "web_search"}):
        result = search_web()
```

See the complete [API and integration examples](usage.md). The optional integrations are installed with `agenturm[langchain]` and `agenturm[openai-agents]`.

For setup examples for Pydantic AI, Google ADK, and any OTLP-compatible agent,
see [Connect agent frameworks](frameworks.md).
