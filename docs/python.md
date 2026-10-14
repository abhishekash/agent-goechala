# Python agents

## Install the CLI

Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/) are required. Install from the repository:

```bash
uv tool install 'git+https://github.com/abhishekash/agent-observatory'
agent-observatory
```

For an existing checkout, run `./scripts/install.sh` (or `scripts/install.ps1` on Windows).

## Observe local Codex and Claude Code sessions

Start `agent-observatory`. Recent local sessions are discovered automatically on the same machine. Press **b** for the web dashboard.

## Automatically instrument a Python SDK agent

Install the automatic capture extra into the same environment as your agent:

```bash
uv pip install --python .venv/bin/python 'agent-observatory[auto]'
```

Run your script through the cockpit:

```bash
agent-observatory cockpit --python .venv/bin/python --service research-agent agent.py
```

Replace the venv path, service name, and script with yours. Use `--module` for a Python module. Script arguments follow the script name. This launcher captures supported OpenAI, Anthropic, and LangChain SDK calls. Prompts and responses are excluded unless `--capture-content` is given.

## Add manual spans

Install the client into the agent environment, then use `Client`:

```python
from observatory import Client

telemetry = Client(service="research-agent")
with telemetry.trace(task="Research a question", session_id="thread-42") as run:
    with run.span("search", attributes={"tool.name": "web_search"}):
        result = search_web()
```

See the complete [API and integration examples](usage.md). The optional integrations are installed with `agent-observatory[langchain]` and `agent-observatory[openai-agents]`.
