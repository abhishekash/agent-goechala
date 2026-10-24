"""Synthetic whole-process resource baseline, isolated from personal sessions.

Run: uv run python scripts/benchmark_app.py
Each scenario runs in its own process with a temporary database, real loopback
receiver/feed, and headless Textual UI. No local discovery or provider calls.
The numbers are descriptive baselines, not portable pass/fail thresholds.
"""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def memory_mib():
    """Current Linux RSS; peak RSS remains separately labelled on other hosts."""
    try:
        rows = Path('/proc/self/status').read_text().splitlines()
        return round(int(next(row for row in rows if row.startswith('VmRSS:')).split()[1]) / 1024, 2)
    except (OSError, StopIteration, ValueError):
        return None


def peak_mib():
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(value / (1024 * 1024 if sys.platform == 'darwin' else 1024), 2)
    except (ImportError, AttributeError):
        return None


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


async def scenario(name, idle_seconds):
    from observatory.core import normalize_native
    from observatory.terminal import Api, Feed, LocalReceiver
    from observatory.terminal_ui import ObservatoryApp

    traces, spans_per_trace, payload_bytes = {
        'small': (10, 20, 0), 'large': (2, 10000, 0),
        'payloads': (2, 1000, 8192),
    }[name]
    # A private scratch directory, not the checkout's or user's telemetry DB.
    with tempfile.TemporaryDirectory(prefix='agenturm-app-benchmark-') as root:
        path = Path(root) / 'synthetic.db'
        receiver = LocalReceiver(path, 0, None, local_agents=False)
        feed = Feed(Api(f'http://127.0.0.1:{receiver.server.server_address[1]}'))
        try:
            start = time.perf_counter()
            base = time.time_ns() - 60_000_000_000
            batch = []
            for trace in range(traces):
                tid = f'{trace + 1:032x}'
                for index in range(spans_per_trace):
                    stamp = base + index * 1_000_000
                    attributes = {'agent.task': f'Synthetic trace {trace}'} if not index else {}
                    if payload_bytes:
                        attributes['synthetic.payload'] = 'x' * payload_bytes
                    batch.append(normalize_native({
                        'trace_id': tid, 'span_id': f'{index + 1:016x}',
                        'parent_span_id': f'{1:016x}' if index else None,
                        'name': 'synthetic.tool' if index else 'synthetic.run',
                        'start_unix_nano': stamp, 'end_unix_nano': stamp + 500_000,
                        'attributes': attributes,
                    }, 'synthetic-benchmark'))
                    if len(batch) == 500:
                        receiver.store.ingest(batch)
                        batch.clear()
            if batch:
                receiver.store.ingest(batch)
            # Drain the derived worker before measuring feed/UI steady state.
            receiver.store.close()
            ingestion_seconds = time.perf_counter() - start
            latencies = []
            for _ in range(12):
                start = time.perf_counter()
                feed.refresh()
                if feed.snapshot.error:
                    raise RuntimeError(feed.snapshot.error)
                latencies.append((time.perf_counter() - start) * 1000)
            app = ObservatoryApp(feed)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                for page in ('activity', 'mcp', 'traces', 'metrics', 'host', 'findings', 'agents'):
                    app.action_page(page)
                    await pilot.pause(.1)
                cpu_start, wall_start = time.process_time(), time.perf_counter()
                await asyncio.sleep(idle_seconds)
                cpu_seconds = time.process_time() - cpu_start
                wall_seconds = time.perf_counter() - wall_start
                rss = memory_mib()
            feed.close()
            db_bytes = sum(item.stat().st_size for item in Path(root).glob('synthetic.db*'))
            peak = peak_mib()
            # Linux's high-water counter and /proc's current RSS are sampled
            # differently; include the observed current sample in the peak.
            if rss is not None:
                peak = max(peak or 0, rss)
            return {
                'scenario': name, 'traces': traces, 'spans': traces * spans_per_trace,
                'payload_bytes_per_span': payload_bytes,
                'ingestion_and_derived_seconds': round(ingestion_seconds, 3),
                'refresh_samples': len(latencies),
                'refresh_median_ms': round(percentile(latencies, .5), 3),
                'refresh_p95_ms': round(percentile(latencies, .95), 3),
                'headless_idle_cpu_percent': round(cpu_seconds / wall_seconds * 100, 2),
                'all_views_warmed': True,
                'idle_seconds': round(wall_seconds, 2), 'current_rss_mib': rss,
                'peak_observed_rss_mib': peak, 'database_mib': round(db_bytes / 1024**2, 3),
            }
        finally:
            feed.close()
            receiver.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=('small', 'large', 'payloads'), help=argparse.SUPPRESS)
    parser.add_argument('--idle-seconds', type=float, default=3)
    args = parser.parse_args()
    if not math.isfinite(args.idle_seconds) or args.idle_seconds <= 0:
        parser.error('--idle-seconds must be positive and finite')
    if args.scenario:
        print(json.dumps(asyncio.run(scenario(args.scenario, args.idle_seconds)), indent=2))
    else:
        results = []
        for name in ('small', 'large', 'payloads'):
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                '--scenario', name, '--idle-seconds', str(args.idle_seconds)],
                check=True, capture_output=True, text=True,
                env={**os.environ, 'OBSERVATORY_QUIET': '1'})
            results.append(json.loads(completed.stdout))
        print(json.dumps({'python': sys.version.split()[0], 'platform': sys.platform,
                          'results': results}, indent=2))


if __name__ == '__main__':
    main()
