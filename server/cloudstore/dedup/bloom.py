"""Bloom filter fronting the chunk index (TDD §6).

Chunk ids are already uniform BLAKE2b digests, so the k probe positions are
derived from the digest itself by double hashing (g_i = h1 + i*h2 mod m) —
no extra hashing on the hot path. A negative answer is definitive and skips the
index lookup entirely; a positive answer falls through to SQLite.
"""

from __future__ import annotations

import math
import struct
import threading
from pathlib import Path

import numpy as np

_HDR = struct.Struct("<8sQIQ")  # magic, m bits, k, items
_MAGIC = b"CSBLOOM1"


class BloomFilter:
    def __init__(self, capacity: int, fpr: float = 0.01, m: int | None = None, k: int | None = None) -> None:
        if m is None:
            m = max(64, int(math.ceil(-capacity * math.log(fpr) / (math.log(2) ** 2))))
        if k is None:
            k = max(1, int(round(m / max(capacity, 1) * math.log(2))))
        self.m = m
        self.k = k
        self.bits = np.zeros((m + 7) // 8, dtype=np.uint8)
        self.count = 0
        self._lock = threading.Lock()
        self.stats = {"negative": 0, "positive": 0, "false_positive": 0}

    def _positions(self, digest: bytes) -> list[int]:
        h1, h2 = struct.unpack_from("<QQ", digest)
        h2 |= 1
        return [(h1 + i * h2) % self.m for i in range(self.k)]

    def add(self, digest: bytes) -> None:
        with self._lock:
            for p in self._positions(digest):
                self.bits[p >> 3] |= 1 << (p & 7)
            self.count += 1

    def __contains__(self, digest: bytes) -> bool:
        bits = self.bits
        for p in self._positions(digest):
            if not bits[p >> 3] & (1 << (p & 7)):
                return False
        return True

    def might_contain(self, digest: bytes) -> bool:
        hit = digest in self
        self.stats["positive" if hit else "negative"] += 1
        return hit

    def expected_fpr(self) -> float:
        return (1 - math.exp(-self.k * self.count / self.m)) ** self.k

    @property
    def size_bytes(self) -> int:
        return len(self.bits)

    # -- persistence -----------------------------------------------------------
    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(_HDR.pack(_MAGIC, self.m, self.k, self.count))
            f.write(self.bits.tobytes())
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "BloomFilter | None":
        try:
            with open(path, "rb") as f:
                magic, m, k, count = _HDR.unpack(f.read(_HDR.size))
                if magic != _MAGIC:
                    return None
                bf = cls(capacity=1, m=m, k=k)
                data = np.frombuffer(f.read(), dtype=np.uint8)
                if len(data) != len(bf.bits):
                    return None
                bf.bits = data.copy()
                bf.count = count
                return bf
        except (OSError, struct.error):
            return None
