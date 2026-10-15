# Storage evaluation: SQLite for live state, DuckDB for optional analytics

Evaluated 2026-10-15. Recommendation: retain SQLite as the live store. Consider DuckDB
for a separate historical analytics feature after workload measurements establish a benefit.
No database migration or new runtime dependency is introduced by this evaluation.

## Observed application workload

`observatory/core.py` uses short transactions, primary-key upserts, per-trace derived
summaries, native record deduplication, and time/session indexes. The local session
watcher, threaded HTTP receiver, retention worker, and derived-summary worker can
access the store concurrently. Terminal refreshes fetch current state and paginated
history; MCP inventory additionally groups stored calls by server and tool.

This is a mixed workload. Session status updates, deduplication, and recent-history
lookups are transactional. Token trends, error rates by model/server, latency percentiles,
and comparisons across months are analytical.

| Requirement | Recommended fit |
| --- | --- |
| Frequent small inserts/upserts and live session reads | Keep SQLite |
| Current MCP listing bugs | Fix adapters, lifecycle correlation, and pagination counts |
| Large historical groupings and percentiles | Evaluate DuckDB |
| Portable analytics archives | Consider Parquet queried by DuckDB |
| Existing installations and minimal setup | Keep SQLite as the default |

## DuckDB tradeoffs

DuckDB's embedded read/write mode allows concurrent writer threads **within one
process**; conflicting updates require retries. Current documentation also describes
multi-process writes through the Quack client/server protocol (documented as beta as
of v1.5.2), and DuckLake with a PostgreSQL catalog. Those options add deployment and
coordination requirements beyond this local embedded application.
[Concurrency documentation](https://duckdb.org/docs/current/connect/concurrency).

DuckDB's SQLite extension can query existing SQLite files and export to Parquet. It
continues to use SQLite locking, so it is not a way to remove write contention. It
also adds extension installation, type-conversion, and distribution considerations.
The documentation warns about linking multiple copies of SQLite into one application.
[SQLite extension](https://duckdb.org/docs/current/core_extensions/sqlite).

## Proposed analytics path

Keep the receiver as the authority for live data. For analytics, create a consistent
SQLite backup using SQLite's backup API (do not copy an active database file blindly),
or export bounded batches to Parquet. Run DuckDB in a separate analytics process over
that snapshot. Show its snapshot timestamp so users can distinguish historical results
from current state. Avoid dual writes and do not make ingestion depend on analytics.

Before adoption, benchmark 100k, 1m, and 10m representative, synthetic events:

- Ingestion p50/p95 latency during dashboard refreshes and analytical scans.
- Latest-session/history reads, full MCP inventory, token trends, and latency percentiles.
- Peak memory, startup latency, disk growth, and export time.
- Concurrent updates, crash/restart recovery, and result equivalence.

First profile existing SQLite queries and indexes. Adopt DuckDB only if measured
analytical gains justify its added dependency and snapshot pipeline. DuckDB is not
installed in the evaluation environment; no comparative performance benchmark was run.
This is an architecture assessment, not a claim that either engine is faster here.
