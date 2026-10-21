# Privacy and capture boundaries

Agenturm is designed for local inspection. The receiver binds to loopback by default and stores telemetry in SQLite on the host running it.

## Native session discovery

On loopback receivers, the local monitor reads recent Codex and Claude Code session files and OpenCode's local SQLite database. It tails session files and opens the OpenCode database read-only. Initial file reads are bounded to recent files and a limited portion of each file; OpenCode discovery is bounded to 50 recent sessions, 30 assistant messages per session, 200 tool parts per message, and 100 todo items per session. Captured activity can include messages, task checklist text, shell commands, file paths and diffs, browser activity, tool inputs/results, and MCP arguments/results. OpenCode reasoning parts are excluded. Metadata-only mode keeps checklist counts and status but drops their text.

The local MCP inventory parses known Codex, Claude Code, Cursor, Gemini CLI, OpenCode, and VS Code configuration files. It supports OpenCode's `opencode.json` and `opencode.jsonc` MCP layouts, including the current nested `mcp.servers` format. For running same-user Gemini CLI, Cursor, and OpenCode processes, it checks the process working directory for the corresponding project config; that directory is used only for the lookup and is not returned. It retains and returns only server names, client names, and configuration scope; command, argument, URL, header, and environment values are discarded and never sent to the receiver API. It does not launch or contact configured servers. Discovery is bounded to known config locations and files up to 1 MiB.

Recognizable credentials and selected sensitive structured fields are redacted before storage on a best-effort basis. Private reasoning text, opaque encrypted reasoning, binary payloads, process environments, and credential files are excluded. Upstream schemas and truncation can leave gaps. Treat the local database as sensitive because normal agent activity may itself contain private project information.

Native activity content can be limited for new ingestion with:

```bash
agenturm serve --metadata-only
```

This does not erase content already stored. To stop native session discovery, use `--no-local-agents`.

## SDK capture

The SDK launcher omits prompts and model responses by default. Content capture is opt-in. The metadata-only setting for native session discovery is separate from SDK capture options.

## Network and retention

The receiver binds to `127.0.0.1` by default. Do not expose it to a network without authentication and a protected TLS/reverse-proxy path. Retention is configurable; see [receiver setup](receiver.md).

Token totals reflect emitted telemetry and may include cached context. Costs remain unknown when the producer does not report them. Neither token nor cost totals should be treated as a billing statement.
