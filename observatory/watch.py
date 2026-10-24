"""Tail active span JSONL files, retaining incomplete lines until they finish."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from .core import normalize_native
from .native_json import loads

MAX_READ = 8 * 1024 * 1024
MAX_LINE = 2 * 1024 * 1024


class JsonlWatcher:
    def __init__(self, store, path, service="agent-harness"):
        self.store = store
        self.path = Path(path)
        self.service = service
        self.offset = 0
        self.file_id = None
        self.pending = b""
        self.discarding_line = False
        self.last_success_ns = 0
        self.backlog_bytes = 0
        self.skipped = 0
        self.skipped_oversized = 0
        self.schema_errors = 0
        self.read_errors = 0

    def diagnostics(self):
        return {"source_path": str(self.path), "last_success_ns": self.last_success_ns,
                "backlog_bytes": self.backlog_bytes, "skipped_records": self.skipped,
                "skipped_oversized_records": self.skipped_oversized,
                "schema_errors": self.schema_errors, "read_errors": self.read_errors}

    def poll(self):
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return 0
        file_id = (stat.st_dev, stat.st_ino)
        if file_id != self.file_id or stat.st_size < self.offset:
            self.offset = 0
            self.pending = b""
            self.discarding_line = False
            self.file_id = file_id
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read(MAX_READ)
            self.offset = stream.tell()
        self.backlog_bytes = max(0, stat.st_size - self.offset)
        if self.discarding_line:
            _, separator, data = data.partition(b"\n")
            if not separator:
                self.last_success_ns = time.time_ns()
                return 0
            self.discarding_line = False
        chunks = (self.pending + data).split(b"\n")
        self.pending = chunks.pop()
        if len(self.pending) > MAX_LINE:
            self.pending = b""
            self.discarding_line = True
            self.skipped += 1
            self.skipped_oversized += 1
        accepted = []
        for line in chunks:
            if len(line) > MAX_LINE:
                self.skipped += 1
                self.skipped_oversized += 1
                continue
            if line.strip():
                try:
                    accepted.append(normalize_native(loads(line), self.service))
                except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
                    self.skipped += 1
                    self.schema_errors += 1
                    print("observatory: skipped malformed span record", file=sys.stderr)
        result = self.store.ingest(accepted) if accepted else 0
        self.last_success_ns = time.time_ns()
        return result


def start_watchers(store, paths, service="agent-harness"):
    stop = threading.Event()
    watchers = [JsonlWatcher(store, path, service) for path in paths]

    def loop():
        while not stop.is_set():
            for watcher in watchers:
                try:
                    watcher.poll()
                except OSError:
                    watcher.read_errors += 1
                    print("observatory: cannot read watched source", file=sys.stderr)
                except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
                    # Defensive boundary for downstream normalization/storage
                    # of producer data: one malformed source must not stop all
                    # watchers. Never print the record or exception text.
                    watcher.schema_errors += 1
                    print("observatory: rejected malformed watched data", file=sys.stderr)
            stop.wait(0.5)

    thread = threading.Thread(target=loop, name="observatory-jsonl-watch", daemon=True)
    thread.start()
    return stop, thread
