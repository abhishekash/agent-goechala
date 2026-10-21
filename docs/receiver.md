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

Optional retention:

```bash
agenturm serve --retention-days 30
agenturm prune --older-than-days 30
```

For complete OTLP field mappings, APIs, collector configuration, and examples, see the [full guide](usage.md).
For framework-specific connection examples, see [Connect agent frameworks](frameworks.md).
