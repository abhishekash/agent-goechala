# Receiver and OTLP

Start the local OTLP/HTTP receiver for the terminal cockpit:

```bash
agenturm serve --port 14318
```

The receiver uses `./observatory.db` unless `--db` selects another path. Its root address is a plain-text status message; inspect sessions in `agenturm`. It accepts OTLP traces, logs, and metrics at `/v1/traces`, `/v1/logs`, and `/v1/metrics` in protobuf or JSON.

Point an OpenTelemetry SDK at the receiver:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:14318
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

The terminal cockpit starts a receiver if needed, and can also attach to an existing one:

```bash
agenturm cockpit --endpoint http://127.0.0.1:14318
```

The receiver binds to loopback by default. For authenticated access, set `OBSERVATORY_TOKEN` in the receiver environment and send a bearer token. Use TLS and a trusted reverse proxy before allowing remote access. The local receiver does not provide multiuser access controls.

The built-in receiver and cockpit receiver admit at most 32 concurrent request
workers. Excess connections receive HTTP 503 with `Retry-After: 1`; exporters
should retry with backoff. Requests have absolute header and body read deadlines
of 10 and 15 seconds, respectively, plus 5-second socket/write timeouts. Stalled
or slow-drip reads receive HTTP 408 when a response can still be sent. Shutdown
interrupts active connections before closing storage. These limits do not make
the local collector an internet-facing service.

### JSONL file watching

Use `--watch-jsonl path/to/spans.jsonl` to follow native span records. Each poll
reads at most 8 MiB; a large backlog is processed over successive polls. Records
larger than 2 MiB are dropped through their terminating newline, including when
the record spans multiple polls. Keep one complete JSON object per line.

### Retention

```bash
agenturm serve --retention-days 30
agenturm prune --older-than-days 30
```

For complete OTLP field mappings, APIs, collector configuration, and examples, see the [full guide](usage.md).
For framework-specific connection examples, see [Connect agent frameworks](frameworks.md).
