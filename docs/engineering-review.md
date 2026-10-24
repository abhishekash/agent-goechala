# Engineering review — 2026-10-24

## Verdict

Agenturm has a good, specific product premise: inspect multiple local agents
without adding a browser, account, or another agent control plane. The strongest
workflow is session → evidence → failed tool call → trace. Preserve that focus.
The synthetic demo, privacy documentation, real Textual capture pipeline, and
interoperability tests are unusually solid foundations for an alpha.

The biggest risk is not missing features. It is trust: showing stale filter
results, following the wrong trace, losing selected-session evidence, or consuming
too many resources while monitoring another application. This pass prioritizes
those problems rather than adding more dashboard views.

## Fixed in this pass

| Area | Problem | Improvement |
| --- | --- | --- |
| Demo identity | Terminal block art did not reproduce the tower logo faithfully | Remove the malformed mark from captures; render the closing identity from the canonical SVG |
| Activity UX | An in-flight request could overwrite a newly selected Prompts filter | Snapshot the filter, discard stale responses, and show a filtering state |
| Trace navigation | Metrics could follow an exemplar from a different instrument | Resolve exemplars within the selected instrument/history |
| Responsive tables | Row previews used old column widths during reflow | Set widths before inserting rows |
| UI work | Session activity was rescanned for every telemetry session | Index latest activity by session once per refresh |
| AI summaries | Limit-before-scope could produce no evidence for the selected session | Scope first; retain only the newest 30 matching events with a bounded heap |
| Local ingestion | A watcher read the entire backlog into memory | Limit reads to 8 MiB per poll and individual records to 2 MiB |
| Record integrity | Oversized partial records could have their suffix parsed as a new record | Discard through the next newline, including across polls |
| Long-running readers | Completed tool input records remained cached | Release them when their result arrives |
| Discovery | All candidate files were retained and sorted | Use a bounded newest-source heap; preserve omission counts |
| Storage queries | Ordered trace pages required a temporary sort | Add a `(trace_id, start_ns, span_id)` index |
| Error inspection | Plain-text tool results could break nested JSON queries | Validate nested JSON before extracting error flags |
| Retention | A concurrent derived refresh could recreate deleted caches | Serialize refresh and pruning within a Store instance |
| Privacy | A new database could briefly inherit permissive umask permissions | Create it with owner-only permissions before SQLite opens it |
| Receiver robustness | Invalid numeric ranges/deep JSON could escape normal rejection | Validate timestamp ranges and return HTTP 400 for overflow/recursion errors |
| Metrics | One-shot iterators lost endpoint inventory updates | Materialize once before the two required traversals |

Oversized watcher records are deliberately dropped; large backlogs drain over
successive polls. These limits are not a claim of a fixed whole-process RSS cap.

## Measurements and their limits

- A synthetic selection benchmark with 100,000 events, one matching session event
  per 100, used **1,600,232 → 3,848 bytes** of peak temporary allocation for the
  selection step. The previous algorithm selected **0** relevant events; the new
  one selected **30**. Measured with `tracemalloc`, excluding the existing input
  list. This is not a whole-application memory measurement.
- The storage reviewer measured an ordered page on a synthetic 50,000-span trace
  at **143.8 → 0.52 ms median** over 25 repetitions. This isolates the query,
  not HTTP latency, ingestion, or end-to-end terminal responsiveness. The new
  regression test verifies index use without asserting a machine-specific time.
- `scripts/benchmark_resources.py` measures OpenCode reader polling only. It
  does not measure the UI or derived Store work; do not present it as the app's
  total resource footprint. In this checkout its synthetic workload measured
  **0.039 ms idle**, **39.512 ms with unrelated database writes**, and
  **79.635 ms with one changed tool** per poll (wall time, eight polls per
  scenario). The latter two paths deserve more attention than the idle fast
  path. These are current-state numbers, not before/after improvements.

## Next priorities identified by the original review

The follow-up implementation status is recorded below; this section preserves
the original findings rather than silently rewriting the review.

### P1: Resource budgets and large traces

`Store._refresh_summaries_locked` still loads complete traces and finding logs.
Pagination bounds the inspector, not this background work. Move aggregate counts
and timestamps into SQL, fetch evidence in bounded windows, and refresh large
traces incrementally. Establish idle CPU, peak RSS, database growth, and p95
refresh budgets using synthetic small/large workloads before tuning defaults.
The UI also rebuilds hidden tabs on refresh, and the feed makes sequential HTTP
requests. Render only dirty/visible views, then consider concurrent independent
reads with a single consistent snapshot publication step. Narrow tables still
need better column prioritization rather than relying on horizontal scrolling.

Local session `started`, `tasks`, and `usage` maps can grow over a long-lived
session. Bound them with lifecycle-aware eviction, preserving cumulative usage
and late tool-result correlation. OpenCode caps record counts, but large JSON
payloads still need byte budgets before loading/decoding.

### P1: Receiver concurrency

The HTTP server still has unbounded request threads and no explicit body-read
deadline. Add bounded concurrency, socket timeouts, and overload responses with
tests for stalled clients. Keep loopback as the default; this is not yet a
hardened internet-facing collector. The retention lock is per Store instance,
not a cross-process consistency guarantee.

### P2: Native schema drift

OpenCode's optional project columns are selected conditionally but accessed
unconditionally. Add partial-schema fixtures and safe fallbacks. Its unchanged-
database shortcut also needs a timed lookback refresh so old sessions age out
without requiring another database write.

### P2: Product polish, not more tabs

- Add a local diagnostics view: source path, last successful read, backlog,
  skipped oversized records, and schema errors, without exposing credentials.
- Persist view/filter/theme preferences without persisting sensitive searches
  by default.
- Offer explicit export of the selected investigation with a privacy preview.
- Make retention discoverable from the cockpit; explain that logical deletion
  is not secure erasure of SQLite free pages or backups.
- Prefer a first-run path that helps users find one real session over adding
  more synthetic dashboard information.

### P2: Architecture and maintainability

`observatory/core.py` mixes normalization, persistence, derived analyses, and
query APIs; `observatory/terminal_ui.py` mixes view-model construction, rendering,
navigation, and asynchronous inspector state. Split along these responsibilities
incrementally, keeping API contracts stable. In particular, represent filter and
selection scope explicitly instead of duplicating equality checks across the
feed and widgets. Add race regressions before moving those responsibilities.
Do not begin with a wholesale rewrite: the existing behavioral tests are an
asset, and native producer compatibility is easy to regress.

Avoid agent approvals, autonomous remediation, or a chat layer until the
monitoring path is measurably reliable. Those features would expand the security
boundary and distract from the app's clearest advantage.

## Follow-up implementation status

This section records what the follow-up pass actually changed. It distinguishes
verified fixes from remaining architectural limits; it is not a claim that every
priority above is closed.

| Area | Status | Implementation |
| --- | --- | --- |
| Large-trace aggregates | Fixed for supported payloads | Exact SQL counts, timestamps, token/cost totals, and ancestry deduplication in `storage_derived.py`; no Python object graph of the whole trace |
| Derived evidence | Bounded and marked incomplete | 256 records, 64 KiB per record, 2 MiB windows, 128 label groups; completeness flags propagate to sessions, metrics, usage series, AI evidence, and the UI |
| Unknown usage | Not fabricated | Oversized/unsupported usage yields `null` with `complete=false`; inspectors and CLI print “Not reported,” not `$0` or `0` tokens |
| Refresh/prune race | Fixed in-process and across Store instances/processes | Refresh and pruning both hold SQLite's writer lock (`BEGIN IMMEDIATE`) while reading and replacing caches; the regression test covers a second `Store` instance |
| Derived statistics storage | Reduced, but larger than `HEAD` | Non-usage spans no longer persist a dictionary of zero fields; the 20k-span synthetic database fell from 14.6 MiB (intermediate implementation) to 9.3 MiB |
| Receiver concurrency | Fixed | `BoundedHTTPServer` admits 32 workers before thread creation, rejects excess connections with 503, enforces absolute header/body deadlines, and interrupts sockets on shutdown |
| Feed reads | Concurrent and bounded | At most four workers and sixteen prefetch slots per refresh; results publish atomically; selection/filter changes discard pending snapshots; responses are capped at 16 MiB |
| Hidden views | Lazy | Only the visible tab renders on refresh; hidden views update when activated |
| Native maps | Bounded | Lifecycle caches use count and byte budgets (64 KiB/entry, 1 MiB/map) with cumulative usage preserved and recent late-result correlation |
| OpenCode payloads | Bounded and fair | 2 MiB per record, 16 MiB per poll, with per-part digests and a resumable cursor so a large record cannot permanently starve later parts, messages, or sessions |
| Schema drift | Hardened | Optional project columns degrade safely; unchanged databases refresh on a 30-second lookback so old sessions age out |
| Malformed native data | Contained | Depth-limited JSON and extreme timestamps cannot terminate watcher or reader threads |
| Diagnostics | Added (`d`) | Content-free reader counters, source paths, backlog, skipped records, schema errors, retention guidance, and first-run help; terminal controls are stripped |
| Preferences | Added and allowlisted | Theme, view, Reviews/Prompts/error flags only; no searches, session IDs, selections, endpoints, or credentials; opt out with `AGENTURM_UI_PREFERENCES=0` |
| Export | Added (`x`) | Frozen privacy preview, explicit confirmation, owner-only new files, no overwrites, coverage disclaimer, selected/loaded evidence only |
| Narrow tables | Improved | Column drop order prioritizes subject/status; full records remain in the inspector |
| AI evidence | Honest about partial data | Completeness flags reach the provider prompt; unknown totals stay `null` |

Measured on this workstation with `scripts/benchmark_app.py --idle-seconds 10`
(Python 3.13.5, Linux, synthetic data, temporary database, all views warmed):

| Workload | Ingest + derived | Refresh median / p95 | Idle CPU | Peak observed RSS | Database |
| --- | --- | --- | --- | --- | --- |
| 10 traces × 20 spans | 0.24 s | 29.9 / 64.6 ms | 6.7% | 68.0 MiB | 0.30 MiB |
| 2 traces × 10,000 spans | 3.0 s | 55.6 / 120.9 ms | 9.6% | 69.7 MiB | 9.34 MiB |
| 2 traces × 1,000 spans, 8 KiB payloads | 1.32 s | 48.7 / 122.6 ms | 8.9% | 81.8 MiB | 17.42 MiB |

At `HEAD` (before this pass) the same large workload measured 69.1 ms median /
180.6 ms p95 refresh, 11.2% idle CPU, 68.8 MiB RSS, and a 5.35 MiB database.
That is a modest refresh-latency improvement, not a dramatic one; the 12-sample
distribution is not a reliable production p95. Idle CPU is dominated by the
headless Textual event loop and loopback polling rather than storage. RSS is
whole-process and includes Python, Textual, SQLite, and temporary allocations.

The database grew to 9.34 MiB because the pass adds a 1.45 MiB trace-start
index and a 2.54 MiB incremental per-span statistics cache (including its
primary-key index). Before compaction, the statistics cache was larger. That
space buys bounded refresh work and faster ordered span pages; it is a real
storage tradeoff, not a free optimization.

### Remaining limits

- Derived refresh still performs whole-trace SQL scans and holds SQLite's writer
  lock. It is bounded in Python memory, not in total database work.
- Findings and search evidence are windowed. Older evidence can be absent and is
  reported as incomplete rather than silently counted as zero.
- Retention and derived-cache replacement are serialized through SQLite's writer
  lock, including across processes. A process that edits the database while
  ignoring SQLite locking, or a filesystem snapshot/backup, is still outside that
  guarantee.
- Receiver limits protect the local collector from accidental overload. They do
  not make it suitable for exposure to an untrusted network.
- Exports are previewed and never overwritten, but they are not a redaction pass;
  recorded prompts, outputs, paths, and producer-supplied secrets can be present.
- Preferences are not encrypted and can reveal which views/filters a user chose.
- OpenCode resumption is bounded per poll; a sufficiently large backlog drains
  over multiple polls rather than instantly.
- Headless UI measurements do not establish rendering behavior in every terminal
  emulator, and clipboard/platform integration was not re-verified.

## Review boundaries

Validation: the final combined run
(`uv run --with cairosvg --with pillow --with imageio-ffmpeg python -m unittest discover -s tests -q`)
completed **257 tests**, with **9 skips**, in about 308 seconds. Focused
storage, feed-concurrency, receiver, native-budget, UI, export, and coverage
suites were also run after their final edits. `git diff --check` and
`compileall` pass. The regenerated GIF's logo and Prompts scene were visually
checked; `scripts/benchmark_app.py` ran all three synthetic workloads in fresh
processes with temporary databases.

This review used the local checkout of the linked repository, synthetic data,
source inspection, and headless terminal tests. It did not inspect personal
session records or benchmark production workloads. Real terminal rendering,
clipboard support, multi-process retention, and all optional SDK integrations
still need platform-specific validation. Changes are local; no release or push
is implied.
