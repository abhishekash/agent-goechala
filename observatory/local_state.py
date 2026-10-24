"""Small insertion/LRU windows for native readers, not session-wide history."""
from collections import OrderedDict
import sys

MAX_MAP_BYTES = 1024 * 1024
MAX_ENTRY_BYTES = 64 * 1024
_MISSING = object()


def _size(*values):
    """Conservative retained Python bytes, including nested keys and values."""
    pending = list(values)
    seen = set()
    total = 256  # Conservative mapping-node and accounting overhead per entry.
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        total += sys.getsizeof(value)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (tuple, list)):
            pending.extend(value)
    return total


class RecentMap(OrderedDict):
    """Bounded recent entries; updating an entry keeps an active stream resident."""

    def __init__(self, limit, *, max_bytes=MAX_MAP_BYTES, max_entry_bytes=MAX_ENTRY_BYTES):
        super().__init__()
        self.limit = limit
        self.evictions = 0
        self.max_bytes = max_bytes
        self.max_entry_bytes = min(max_entry_bytes, max_bytes)
        self.tracked_bytes = 0
        self.dropped_oversized = 0
        self._sizes = {}

    def __setitem__(self, key, value):
        size = _size(key, value)
        if key in self:
            del self[key]
        if size > self.max_entry_bytes:
            # Never truncate identifiers: that could correlate unrelated tools.
            self.dropped_oversized += 1
            return
        super().__setitem__(key, value)
        self._sizes[key] = size
        self.tracked_bytes += size
        self.move_to_end(key)
        while len(self) > self.limit or self.tracked_bytes > self.max_bytes:
            self.popitem(last=False)
            self.evictions += 1

    def __delitem__(self, key):
        super().__delitem__(key)
        self.tracked_bytes -= self._sizes.pop(key)

    def pop(self, key, default=_MISSING):
        if key in self:
            value = self[key]
            del self[key]
            return value
        if default is _MISSING:
            raise KeyError(key)
        return default

    def popitem(self, last=True):
        key, value = super().popitem(last=last)
        self.tracked_bytes -= self._sizes.pop(key)
        return key, value

    def clear(self):
        super().clear()
        self._sizes.clear()
        self.tracked_bytes = 0

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        else:
            self.move_to_end(key)
        return self.get(key, default)
