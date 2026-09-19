"""Size-aware LRU cache for thumbnails (TDD §10).

Capacity is a byte budget, not an item count, so a few large proxies cannot
silently evict hundreds of small thumbnails. O(1) get/put via OrderedDict.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Generic, Hashable, TypeVar

K = TypeVar("K", bound=Hashable)


class SizeAwareLRU(Generic[K]):
    def __init__(self, budget_bytes: int) -> None:
        self.budget = budget_bytes
        self.used = 0
        self._d: OrderedDict[K, bytes] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = self.evictions = 0

    def get(self, key: K) -> bytes | None:
        with self._lock:
            v = self._d.get(key)
            if v is None:
                self.misses += 1
                return None
            self._d.move_to_end(key)
            self.hits += 1
            return v

    def put(self, key: K, value: bytes) -> None:
        n = len(value)
        if n > self.budget:
            return
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self.used -= len(old)
            self._d[key] = value
            self.used += n
            while self.used > self.budget:
                _, v = self._d.popitem(last=False)
                self.used -= len(v)
                self.evictions += 1

    def __contains__(self, key: K) -> bool:
        return key in self._d

    def __len__(self) -> int:
        return len(self._d)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"items": len(self._d), "used_bytes": self.used, "budget_bytes": self.budget,
                "hits": self.hits, "misses": self.misses, "evictions": self.evictions,
                "hit_rate": self.hits / total if total else None}
