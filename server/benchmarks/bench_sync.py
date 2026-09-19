"""Merkle sync vs full-manifest comparison: bytes per sync check (TDD §12.6).

Replays the exact HTTP protocol of api/sync.py against in-memory trees and counts
the JSON payload bytes both directions. The baseline sends the full manifest
(every hash) each sync.
"""

from __future__ import annotations

import hashlib
import json

from cloudstore.sync.merkle import DEPTH, HEX, MerkleTree

from .common import table


def _h(i: int) -> str:
    return hashlib.sha256(i.to_bytes(8, "little")).hexdigest()


def sync_bytes(server: MerkleTree, client: MerkleTree) -> tuple[int, int]:
    """(bytes, round trips) for one sync check using the Merkle protocol."""
    total = len(json.dumps({"root": server.root().hex(), "count": len(server), "depth": DEPTH}))
    trips = 1
    if server.root() == client.root():
        return total, trips
    frontier = [""]
    for _ in range(DEPTH):
        req = json.dumps({"paths": frontier})
        resp = json.dumps({"nodes": {p: [h.hex() for h in server.children(p)] for p in frontier}})
        total += len(req) + len(resp)
        trips += 1
        nxt = []
        for p in frontier:
            for c, h in zip(HEX, server.children(p)):
                if client.node(p + c) != h:
                    nxt.append(p + c)
        frontier = nxt
    req = json.dumps({"paths": frontier})
    resp = json.dumps({"buckets": {p: server.bucket(p) for p in frontier}})
    return total + len(req) + len(resp), trips + 1


def run() -> str:
    rows = []
    for n in (1_000, 10_000, 100_000):
        base = [_h(i) for i in range(n)]
        server = MerkleTree(base)
        full = len(json.dumps({"hashes": base}))
        for changes in (0, 1, 10, 100):
            client = MerkleTree(base + [_h(n + i) for i in range(changes)])
            b, trips = sync_bytes(server, client)
            rows.append([f"{n:,}", changes, b, full, full / b, trips])
    return table(["items", "new photos", "Merkle bytes", "full-manifest bytes", "reduction ×", "round trips"], rows)
