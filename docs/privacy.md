# Privacy and capture boundaries

Agent Observatory is designed for local inspection. The receiver binds to loopback by default and stores telemetry in SQLite on the host running it.

## Native session discovery

On loopback receivers, the local monitor reads recent Codex and Claude Code session files. It tails files rather than modifying them. Initial reads are bounded to recent files and a limited portion of each file. Captured activity can include messages, shell commands, file paths and diffs, browser activity, tool inputs/results, and MCP arguments/results.

Recognizable credentials and selected sensitive structured fields are redacted before storage on a best-effort basis. Private reasoning text, opaque encrypted reasoning, binary payloads, process environments, and credential files are excluded. Upstream schemas and truncation can leave gaps. Treat the local database as sensitive because normal agent activity may itself contain private project information.

Native activity content can be limited for new ingestion with:

```bash
agent-observatory serve --metadata-only
```

This does not erase content already stored. To stop native session discovery, use `--no-local-agents`.

## SDK capture

The SDK launcher omits prompts and model responses by default. Content capture is opt-in. The metadata-only setting for native session discovery is separate from SDK capture options.

## Network and retention

The receiver binds to `127.0.0.1` by default. Do not expose it to a network without authentication and a protected TLS/reverse-proxy path. Retention is configurable; see [receiver setup](receiver.md).

Token totals reflect emitted telemetry and may include cached context. Costs remain unknown when the producer does not report them. Neither token nor cost totals should be treated as a billing statement.
