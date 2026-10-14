"""Tail active span JSONL files, retaining incomplete lines until they finish."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from .core import normalize_native


class JsonlWatcher:
    def __init__(self, store, path, service="agent-harness"):
        self.store = store
        self.path = Path(path)
        self.service = service
        self.offset = 0
        self.file_id = None
        self.pending = b""

    def poll(self):
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return 0
        file_id = (stat.st_dev, stat.st_ino)
        if file_id != self.file_id or stat.st_size < self.offset:
            self.offset = 0
            self.pending = b""
            self.file_id = file_id
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read()
            self.offset = stream.tell()
        chunks = (self.pending + data).split(b"\n")
        self.pending = chunks.pop()
        accepted = []
        for line in chunks:
            if line.strip():
                try:
                    accepted.append(normalize_native(json.loads(line), self.service))
                except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
                    print(f"observatory: skipped malformed span in {self.path}: {exc}", file=sys.stderr)
        return self.store.ingest(accepted) if accepted else 0


def start_watchers(store, paths, service="agent-harness"):
    stop = threading.Event()
    watchers = [JsonlWatcher(store, path, service) for path in paths]

    def loop():
        while not stop.is_set():
            for watcher in watchers:
                try:
                    watcher.poll()
                except OSError as exc:
                    print(f"observatory: cannot read {watcher.path}: {exc}", file=sys.stderr)
            stop.wait(0.5)

    thread = threading.Thread(target=loop, name="observatory-jsonl-watch", daemon=True)
    thread.start()
    return stop, thread
