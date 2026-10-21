"""Benchmark OpenCode polling using temporary synthetic data, never user sessions.

Run: python scripts/benchmark_resources.py
Measures reader CPU/IO and write volume, not the terminal or derived Store work.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observatory.opencode import OpenCodeReader


class CountingStore:
    def __init__(self):
        self.spans = 0

    def ingest(self, spans):
        self.spans += len(spans)

    def ingest_logs(self, logs):
        pass


def logical_reads():
    try:
        return int(dict(line.split(':', 1) for line in
                        Path('/proc/self/io').read_text().splitlines())['rchar'])
    except (OSError, KeyError):
        return 0


def benchmark(reader_class=OpenCodeReader):
    results = {}
    with tempfile.TemporaryDirectory(prefix='agenturm-benchmark-') as root:
        path = Path(root) / 'synthetic.db'
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''
                CREATE TABLE session(id TEXT PRIMARY KEY, title TEXT, directory TEXT,
                    time_created INTEGER, time_updated INTEGER);
                CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
                    time_updated INTEGER, data TEXT);
                CREATE INDEX messages_session ON message(session_id,time_created);
                CREATE TABLE part(id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT,
                    time_created INTEGER, time_updated INTEGER, data TEXT);
                CREATE INDEX parts_message ON part(session_id,message_id,time_created);
                CREATE TABLE unrelated(value INTEGER);
                INSERT INTO unrelated VALUES(0);
            ''')
            now = time.time_ns() // 1_000_000
            for session in range(12):
                sid = f'session-{session}'
                db.execute('INSERT INTO session VALUES(?,?,?,?,?)',
                           (sid, 'Synthetic task', '/synthetic/project', now-1000, now))
                for message in range(12):
                    mid = f'{sid}-message-{message}'
                    data = json.dumps({'role': 'assistant', 'modelID': 'synthetic-model',
                                       'tokens': {'input': 10, 'output': 5}, 'finish': 'stop'})
                    db.execute('INSERT INTO message VALUES(?,?,?,?,?)', (mid, sid, now, now, data))
                    for part in range(6):
                        data = json.dumps({'type': 'tool', 'tool': 'synthetic-tool',
                                           'state': {'status': 'completed', 'output': 'x' * 8192}})
                        db.execute('INSERT INTO part VALUES(?,?,?,?,?,?)',
                                   (f'{mid}-part-{part}', sid, mid, now, now, data))
        store = CountingStore()
        reader = reader_class(store, path=path)
        reader.poll()  # Initial import, excluded from steady-state measurements.
        with closing(sqlite3.connect(path)) as db:
            for scenario in ('idle', 'unrelated_writes', 'one_changed_tool'):
                cpu = wall = reads = 0
                before_spans = store.spans
                for iteration in range(8):
                    if scenario == 'unrelated_writes':
                        db.execute('UPDATE unrelated SET value=value+1')
                    elif scenario == 'one_changed_tool':
                        db.execute('UPDATE part SET time_updated=time_updated+1 WHERE id=?',
                                   ('session-0-message-0-part-0',))
                    db.commit()
                    before_reads = logical_reads()
                    start_cpu, start_wall = time.process_time(), time.perf_counter()
                    reader.poll()
                    cpu += time.process_time() - start_cpu
                    wall += time.perf_counter() - start_wall
                    reads += max(0, logical_reads() - before_reads)
                results[scenario] = {'cpu_ms_per_poll': round(cpu * 1000 / 8, 3),
                                     'wall_ms_per_poll': round(wall * 1000 / 8, 3),
                                     'logical_kib_per_poll': round(reads / 1024 / 8, 1),
                                     'spans_written': store.spans - before_spans}
    return results


if __name__ == '__main__':
    print(json.dumps(benchmark(), indent=2))
