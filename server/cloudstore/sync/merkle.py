"""Merkle-tree sync reconciliation (TDD §7).

Fixed-depth 16-ary trie over the hex digits of SHA-256 content hashes.
  * leaf (depth 3, path like "a3f")  = SHA-256(sorted raw 32-byte member hashes)
  * internal node                     = SHA-256(concatenation of 16 child hashes)
  * any empty subtree                 = 32 zero bytes
The Swift client (ios/CloudSyncKit) implements the same function; shared test
vectors live in spec/merkle_vectors.json.

Client and server compare roots, then descend only into differing children, so a
sync check costs O(changed buckets × (depth + bucket size)) instead of O(n).
"""

from __future__ import annotations

import hashlib
import threading
from typing import Callable, Iterable

DEPTH = 3
HEX = "0123456789abcdef"
EMPTY = bytes(32)


class MerkleTree:
    def __init__(self, hashes: Iterable[str] = ()) -> None:
        self.buckets: dict[str, set[bytes]] = {}
        self._cache: dict[str, bytes] = {}
        self._lock = threading.RLock()
        for h in hashes:
            self.add(h)

    def __len__(self) -> int:
        return sum(len(b) for b in self.buckets.values())

    def __contains__(self, h: str) -> bool:
        b = self.buckets.get(h[:DEPTH])
        return b is not None and bytes.fromhex(h) in b

    def _invalidate(self, path: str) -> None:
        for i in range(DEPTH + 1):
            self._cache.pop(path[:i], None)

    def add(self, h: str) -> bool:
        h = h.lower()
        raw = bytes.fromhex(h)
        if len(raw) != 32:
            raise ValueError("expected a SHA-256 hex digest")
        with self._lock:
            b = self.buckets.setdefault(h[:DEPTH], set())
            if raw in b:
                return False
            b.add(raw)
            self._invalidate(h[:DEPTH])
            return True

    def remove(self, h: str) -> bool:
        h = h.lower()
        with self._lock:
            b = self.buckets.get(h[:DEPTH])
            raw = bytes.fromhex(h)
            if not b or raw not in b:
                return False
            b.remove(raw)
            if not b:
                del self.buckets[h[:DEPTH]]
            self._invalidate(h[:DEPTH])
            return True

    def node(self, path: str) -> bytes:
        with self._lock:
            cached = self._cache.get(path)
            if cached is not None:
                return cached
            if len(path) == DEPTH:
                members = self.buckets.get(path)
                val = hashlib.sha256(b"".join(sorted(members))).digest() if members else EMPTY
            else:
                kids = [self.node(path + c) for c in HEX]
                val = EMPTY if all(k == EMPTY for k in kids) else hashlib.sha256(b"".join(kids)).digest()
            self._cache[path] = val
            return val

    def root(self) -> bytes:
        return self.node("")

    def children(self, path: str) -> list[bytes]:
        if len(path) >= DEPTH:
            raise ValueError("leaf has no children")
        return [self.node(path + c) for c in HEX]

    def bucket(self, path: str) -> list[str]:
        if len(path) != DEPTH:
            raise ValueError(f"bucket path must have {DEPTH} hex digits")
        with self._lock:
            return sorted(h.hex() for h in self.buckets.get(path, ()))


def diff(local: MerkleTree,
         remote_root: bytes,
         remote_children: Callable[[list[str]], dict[str, list[bytes]]],
         remote_buckets: Callable[[list[str]], dict[str, list[str]]]) -> tuple[set[str], set[str]]:
    """Client-side reconciliation. Returns (missing_on_remote, extra_on_remote).

    ``remote_children`` / ``remote_buckets`` are batched calls (one round trip per
    tree level), matching the HTTP protocol in api/sync.py.
    """
    if local.root() == remote_root:
        return set(), set()
    frontier = [""]
    for _ in range(DEPTH):
        remote = remote_children(frontier)
        nxt = []
        for p in frontier:
            for c, rh in zip(HEX, remote[p]):
                if local.node(p + c) != rh:
                    nxt.append(p + c)
        frontier = nxt
        if not frontier:
            return set(), set()
    remote_members = remote_buckets(frontier)
    missing, extra = set(), set()
    for p in frontier:
        mine = set(local.bucket(p))
        theirs = set(remote_members[p])
        missing |= mine - theirs
        extra |= theirs - mine
    return missing, extra
