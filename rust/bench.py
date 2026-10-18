"""Compare complete import-to-query time with synthetic native spans.

Build first: cargo build --release --manifest-path rust/Cargo.toml
Run from the repository root: python rust/bench.py
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from observatory.core import Store, normalize_native


ROWS = 20_000
TRACES = 100
BINARY = ROOT / "rust/target/release/agent-observatory-rs"


def fixture(path):
    with path.open("w", encoding="utf-8") as stream:
        for index in range(ROWS):
            trace_id = f"{index % TRACES + 1:032x}"
            span_id = f"{index // TRACES + 1:016x}"
            span = {"trace_id": trace_id, "span_id": span_id,
                    "name": "agent.run" if index < TRACES else "tool.call",
                    "start_unix_nano": 1_000_000_000 + index * 1000,
                    "end_unix_nano": 1_000_000_100 + index * 1000,
                    "attributes": {"agent.task": "benchmark"}, "events": []}
            stream.write(json.dumps(span, separators=(",", ":")) + "\n")


def run():
    if not BINARY.is_file():
        raise SystemExit(f"Build the release binary first: {BINARY}")
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        source = root / "spans.jsonl"
        fixture(source)

        start = time.perf_counter()
        with source.open(encoding="utf-8") as stream:
            spans = [normalize_native(json.loads(line)) for line in stream]
        python_parse_seconds = time.perf_counter() - start
        python_store = Store(root / "python.db")
        python_store.ingest(spans)
        python_runs = python_store.traces(limit=TRACES)
        python_seconds = time.perf_counter() - start

        start = time.perf_counter()
        subprocess.run([str(BINARY), "--db", str(root / "rust.db"),
                        "import-jsonl", str(source)], check=True, capture_output=True)
        rust_import_seconds = time.perf_counter() - start
        # Both paths include the existing derived summaries before querying.
        rust_store = Store(root / "rust.db")
        rust_runs = rust_store.traces(limit=TRACES)
        rust_seconds = time.perf_counter() - start
        assert len(python_runs) == len(rust_runs) == TRACES
        assert sum(run["span_count"] for run in python_runs) == ROWS
        assert sum(run["span_count"] for run in rust_runs) == ROWS
        print(f"{ROWS:,} spans / {TRACES} traces, import plus query-ready summaries")
        print(f"Python: {python_seconds:.3f} s")
        print(f"  parse/normalize: {python_parse_seconds:.3f} s")
        print(f"Rust + Python summary backfill: {rust_seconds:.3f} s")
        print(f"  Rust import: {rust_import_seconds:.3f} s")


if __name__ == "__main__":
    run()
