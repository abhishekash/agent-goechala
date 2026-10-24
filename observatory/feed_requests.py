"""Bounded concurrent reads and immutable scope for a single feed refresh."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


@dataclass(frozen=True)
class FeedScope:
    service: str
    session_id: str
    query: str
    activity_query: str
    mcp_query: str
    errors_only: bool
    include_reviews: bool
    activity_prompts: bool
    selected_trace: str | None
    selected_span_id: str | None
    pinned_trace: bool

    @classmethod
    def capture(cls, feed):
        return cls(**{name: getattr(feed, name) for name in cls.__dataclass_fields__})


class RequestBatch:
    """Prefetch only known independent reads; dependent reads remain explicit.

    Workers never mutate the feed or publish snapshots. All results are consumed
    by the polling thread, and outstanding reads are cancelled on shutdown.
    """
    MAX_WORKERS = 4
    MAX_PREFETCH = 16

    def __init__(self, api, stop):
        self.api, self.stop = api, stop
        self.executor = None
        self.pending = {}

    def prefetch(self, paths):
        for path in paths:
            if self.stop.is_set():
                break
            if path in self.pending:
                continue
            if len(self.pending) >= self.MAX_PREFETCH:
                raise ValueError('feed prefetch budget exceeded')
            if self.executor is None:
                self.executor = ThreadPoolExecutor(max_workers=self.MAX_WORKERS,
                                                   thread_name_prefix='agenturm-read')
            self.pending[path] = self.executor.submit(self.api.get, path)

    def get(self, path):
        if self.stop.is_set():
            raise InterruptedError('feed stopped')
        future = self.pending.get(path)
        return future.result() if future is not None else self.api.get(path)

    def cancel(self):
        for future in tuple(self.pending.values()):
            future.cancel()

    def close(self):
        self.cancel()
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)
