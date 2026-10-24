# Resource budgets and reproducible benchmarks

Agenturm monitors other processes, so bounded background work matters as much
as the speed of a single inspector. Use synthetic workloads for comparisons;
do not copy personal agent databases into benchmark fixtures.

## Whole-app baseline

```bash
uv run python scripts/benchmark_app.py
# Longer idle window, less affected by UI startup:
uv run python scripts/benchmark_app.py --idle-seconds 30
```

The benchmark runs each workload in a fresh process and temporary directory:

- Small: 10 traces with 20 spans each.
- Large: 2 traces with 10,000 spans each.
- Payloads: 2 traces with 1,000 spans each, with 8 KiB synthetic payloads per span.
- Actual SQLite storage, loopback receiver, HTTP feed, and headless Textual UI;
  all seven views are visited before measuring idle CPU and RSS.
- No local session discovery, personal preference reads/writes, paid models,
  or external network calls.

It reports ingestion plus derived-processing time, feed refresh latency, idle
process CPU, current Linux RSS, peak observed RSS where supported, and database
size. CPU includes the UI, receiver, and feed. Twelve refresh samples provide a
small empirical distribution, not a statistically reliable production p95;
compare on the same machine with the same Python version. Headless UI memory
and CPU do not establish rendering performance for every terminal emulator.
RSS includes Python, Textual, SQLite, and temporary allocations: it is not just
the size of persisted telemetry.
Peak observed RSS is the larger of the OS high-water counter and the sampled
current RSS; those OS accounting mechanisms can differ slightly.

## Native-reader microbenchmark

```bash
uv run python scripts/benchmark_resources.py
```

This isolates OpenCode reader polling for idle, unrelated database writes, and
one changed tool. It does **not** measure derived storage processing or UI work.
Avoid treating this microbenchmark as whole-app performance.

## Deterministic budgets

Regression tests enforce work limits rather than machine-dependent timings:

- Feed reads have at most four workers and sixteen prefetch slots per refresh.
  Workers do not publish snapshots. A changed filter or selection invalidates
  the pending snapshot.
- Each receiver response is limited to 16 MiB before JSON decoding. Decoded
  Python objects can require more memory than their serialized representation.
- Live feed polling waits two seconds between refreshes. Inventory and findings
  reads are cached for five seconds unless their scope changes.
- Watched JSONL reads are limited to 8 MiB per poll and 2 MiB per record.
  Oversized records are dropped through their terminating newline.
- OpenCode payloads are limited to 2 MiB per record and 16 MiB per poll before
  JSON is transferred into Python; deferred parts resume on later polls.
- Native lifecycle caches are limited to 64 KiB per entry and 1 MiB per map.
- AI summary evidence retains at most thirty matching events.

These are component budgets, not a fixed whole-process RSS ceiling. Before
changing defaults, compare all workloads, inspect database growth, and check
that the optimization has not changed counts, correlation, or error semantics.
