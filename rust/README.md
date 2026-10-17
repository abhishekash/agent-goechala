# Rust port: trace ingestion

This is the first native component of Agent Observatory. It is a working Rust
JSONL importer and loopback HTTP trace receiver that writes the existing SQLite
schema. The Python dashboard can read the resulting database. The normal
`agent-observatory` command remains the supported full application.

## Build and use

Install a Rust toolchain, then from the repository root:

```bash
cargo build --release --manifest-path rust/Cargo.toml
rust/target/release/agent-observatory-rs --db runs.db import-jsonl traces.jsonl
rust/target/release/agent-observatory-rs --db runs.db serve --port 4318
```

The receiver accepts `POST /api/spans` with native JSON spans and
`POST /v1/traces` with OTLP JSON. It accepts gzip request bodies, bounds the
compressed and decompressed body to 8 MiB, and supports `--token` or
`OBSERVATORY_TOKEN` for bearer authentication. It listens on `127.0.0.1` by
default. `GET /api/health` reports receiver health.

Run the Python dashboard against the same database after an import. Do not run
both receivers on the same port. The Rust process does not yet serve the web
dashboard or implement local Codex and Claude Code session discovery, OTLP
protobuf, logs, metrics, progress, outcomes, or the full query API. It is not a
drop-in replacement for `agent-observatory serve`.

## Validation and performance

```bash
cargo test --manifest-path rust/Cargo.toml
python rust/compat.py
python rust/bench.py
```

The compatibility check verifies that Python reads Rust-imported traces and
endpoint inventory, and that reimporting a span invalidates the cached summary.
The benchmark includes parsing, storage, derived summaries, and a query of
all traces. On one local run with 20,000 synthetic spans across 100 traces,
Python took 0.78 seconds; Rust import followed by Python summary backfill took
0.62 seconds. This is a modest improvement for that workload, not evidence of
a generally faster app. The Rust import itself took 0.18 seconds. Run repeated
benchmarks with representative traces and live dashboard traffic before
making a performance claim.

The next porting steps are derived summaries and findings, protobuf plus
logs/metrics, the query API, local session discovery, and terminal UI. Keep
cross-language database checks as each component moves.
