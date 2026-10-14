# Receiver and OTLP

Start the local web dashboard and OTLP/HTTP receiver:

```bash
agent-observatory serve --port 14318
```

Open `http://127.0.0.1:14318`. The receiver uses `./observatory.db` unless `--db` selects another path. It accepts OTLP traces, logs, and metrics at `/v1/traces`, `/v1/logs`, and `/v1/metrics` in protobuf or JSON.

Point an OpenTelemetry SDK at the receiver:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:14318
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

The terminal cockpit starts a receiver if needed, and can also attach to an existing one:

```bash
agent-observatory cockpit --endpoint http://127.0.0.1:14318
```

The receiver binds to loopback by default. For authenticated access, set `OBSERVATORY_TOKEN` in the receiver environment and send a bearer token. Use TLS and a trusted reverse proxy before allowing remote access. The local receiver does not provide multiuser access controls.

Optional retention:

```bash
agent-observatory serve --retention-days 30
agent-observatory prune --older-than-days 30
```

For complete OTLP field mappings, APIs, collector configuration, and examples, see the [full guide](usage.md).
