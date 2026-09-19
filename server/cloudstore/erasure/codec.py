"""Reed–Solomon codec: systematic Cauchy RS(k, m) over GF(2^8) (TDD §4.2–4.3)."""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from typing import Sequence

import numpy as np

from . import matrix as mx
from .backends import Backend, get_backend

SHARD_ALIGN = 64


class NotEnoughShards(Exception):
    """Fewer than k valid shards survive: the stripe cannot be reconstructed."""


class ReedSolomon:
    def __init__(self, k: int, m: int, backend: Backend | str | None = None) -> None:
        if k < 1 or m < 0 or k + m > 256:
            raise ValueError(f"invalid RS parameters k={k} m={m}")
        self.k = k
        self.m = m
        self.n = k + m
        self.backend = backend if isinstance(backend, Backend) else get_backend(backend)
        self.matrix = mx.encoding_matrix(k, m)
        self.parity_rows = self.matrix[k:]
        self._inv_cache: OrderedDict[tuple[int, ...], mx.Matrix] = OrderedDict()
        self._lock = Lock()

    # -- layout helpers ---------------------------------------------------
    def shard_size_for(self, length: int, max_shard: int | None = None) -> int:
        size = -(-length // self.k) if length else SHARD_ALIGN
        size = -(-size // SHARD_ALIGN) * SHARD_ALIGN
        if max_shard is not None:
            size = min(size, max_shard)
        return size

    def split(self, data: bytes | memoryview, shard_size: int) -> list[np.ndarray]:
        """Split up to k*shard_size bytes into k zero-padded data shards."""
        buf = np.zeros(self.k * shard_size, dtype=np.uint8)
        view = np.frombuffer(data, dtype=np.uint8)
        buf[: len(view)] = view
        return [buf[i * shard_size : (i + 1) * shard_size] for i in range(self.k)]

    # -- encode -------------------------------------------------------------
    def encode(self, data_shards: Sequence[np.ndarray]) -> list[np.ndarray]:
        """Return the m parity shards for k equal-length data shards."""
        if len(data_shards) != self.k:
            raise ValueError(f"expected {self.k} data shards, got {len(data_shards)}")
        if self.m == 0:
            return []
        return self.backend.matmul(self.parity_rows, data_shards)

    def encode_bytes(self, data: bytes | memoryview, shard_size: int | None = None) -> list[np.ndarray]:
        """Split + encode: return all k+m shards for one stripe."""
        shard_size = shard_size or self.shard_size_for(len(data))
        data_shards = self.split(data, shard_size)
        return data_shards + self.encode(data_shards)

    # -- decode -------------------------------------------------------------
    def _decode_matrix(self, present: tuple[int, ...]) -> mx.Matrix:
        with self._lock:
            cached = self._inv_cache.get(present)
            if cached is not None:
                self._inv_cache.move_to_end(present)
                return cached
        sub = [self.matrix[i] for i in present]
        inv = mx.invert(sub)
        with self._lock:
            self._inv_cache[present] = inv
            if len(self._inv_cache) > 512:
                self._inv_cache.popitem(last=False)
        return inv

    def choose_survivors(self, available: Sequence[int]) -> tuple[int, ...]:
        """Pick k survivor indices, preferring data shards (cheaper decode)."""
        avail = sorted(set(available))
        if len(avail) < self.k:
            raise NotEnoughShards(f"need {self.k} shards, have {len(avail)}")
        data = [i for i in avail if i < self.k]
        parity = [i for i in avail if i >= self.k]
        return tuple((data + parity)[: self.k])

    def reconstruct_data(self, shards: dict[int, np.ndarray]) -> list[np.ndarray]:
        """Given >= k shards by index, return the k data shards."""
        present = self.choose_survivors(list(shards))
        if present == tuple(range(self.k)):
            return [shards[i] for i in range(self.k)]
        inv = self._decode_matrix(present)
        missing = [i for i in range(self.k) if i not in present]
        rows = [inv[i] for i in missing]
        recovered = self.backend.matmul(rows, [shards[i] for i in present])
        out = dict(zip(missing, recovered))
        return [shards[i] if i in present else out[i] for i in range(self.k)]

    def reconstruct(self, shards: dict[int, np.ndarray], wanted: Sequence[int] | None = None) -> dict[int, np.ndarray]:
        """Rebuild the shards in ``wanted`` (default: all missing ones), data or parity."""
        wanted = [i for i in (wanted if wanted is not None else range(self.n)) if i not in shards]
        if not wanted:
            return {}
        data = self.reconstruct_data(shards)
        result: dict[int, np.ndarray] = {}
        for i in wanted:
            if i < self.k:
                result[i] = data[i]
        parity_wanted = [i for i in wanted if i >= self.k]
        if parity_wanted:
            rows = [self.matrix[i] for i in parity_wanted]
            for i, arr in zip(parity_wanted, self.backend.matmul(rows, data)):
                result[i] = arr
        return result

    def decode_bytes(self, shards: dict[int, np.ndarray], length: int) -> bytes:
        data = self.reconstruct_data(shards)
        return b"".join(d.tobytes() for d in data)[:length]
