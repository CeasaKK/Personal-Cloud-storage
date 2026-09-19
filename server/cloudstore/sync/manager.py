"""Per-device sync manifests backed by ``device_items`` (TDD §7).

The in-memory Merkle tree of each device is built lazily from the table and
kept in step with every link/unlink, so root/node queries are O(1) after the
first request.
"""

from __future__ import annotations

import threading
import time
import uuid

from ..db.database import Database
from .merkle import MerkleTree


class SyncManager:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._trees: dict[str, MerkleTree] = {}
        self._lock = threading.Lock()

    # -- devices --------------------------------------------------------------
    def register_device(self, name: str, platform: str, device_id: str | None = None) -> str:
        device_id = device_id or uuid.uuid4().hex
        now = time.time()
        self.db.execute(
            "INSERT INTO devices(id, name, platform, created_at, last_seen_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, last_seen_at = excluded.last_seen_at",
            (device_id, name, platform, now, now))
        return device_id

    def device_exists(self, device_id: str) -> bool:
        return self.db.scalar("SELECT 1 FROM devices WHERE id = ?", (device_id,)) is not None

    def devices(self) -> list[dict]:
        rows = self.db.all(
            "SELECT d.*, (SELECT COUNT(*) FROM device_items i WHERE i.device_id = d.id) AS items "
            "FROM devices d ORDER BY d.created_at")
        return [dict(r) for r in rows]

    # -- trees ----------------------------------------------------------------
    def tree(self, device_id: str) -> MerkleTree:
        with self._lock:
            t = self._trees.get(device_id)
            if t is None:
                rows = self.db.all("SELECT sha256 FROM device_items WHERE device_id = ?", (device_id,))
                t = self._trees[device_id] = MerkleTree(r[0] for r in rows)
            return t

    def link(self, device_id: str, hashes: list[str]) -> int:
        if not hashes:
            return 0
        now = time.time()
        with self.db.tx() as c:
            c.executemany("INSERT OR IGNORE INTO device_items(device_id, sha256, added_at) VALUES(?,?,?)",
                          [(device_id, h, now) for h in hashes])
        t = self.tree(device_id)
        return sum(t.add(h) for h in hashes)

    def unlink(self, device_id: str, hashes: list[str]) -> int:
        if not hashes:
            return 0
        with self.db.tx() as c:
            c.executemany("DELETE FROM device_items WHERE device_id = ? AND sha256 = ?",
                          [(device_id, h) for h in hashes])
        t = self.tree(device_id)
        return sum(t.remove(h) for h in hashes)

    def reconcile(self, device_id: str, add: list[str], remove: list[str]) -> dict:
        """Apply the client's diff. Hashes the server already knows (stored, or
        tombstoned after a deliberate delete) are linked without upload; the rest
        are returned as ``need_upload``."""
        add = sorted({h.lower() for h in add})
        remove = sorted({h.lower() for h in remove})
        known: set[str] = set()
        for i in range(0, len(add), 500):
            part = add[i : i + 500]
            rows = self.db.all("SELECT sha256 FROM files WHERE sha256 IN (%s)" % ",".join("?" * len(part)), part)
            known |= {r[0] for r in rows}
        linked = self.link(device_id, sorted(known))
        self.unlink(device_id, remove)
        self.db.execute("UPDATE devices SET last_sync_at = ?, last_seen_at = ? WHERE id = ?",
                        (time.time(), time.time(), device_id))
        need = [h for h in add if h not in known]
        pending = set()
        if need:
            rows = self.db.all("SELECT sha256 FROM ingest_jobs WHERE state IN ('queued','running')")
            pending = {r[0] for r in rows}
        return {"linked": linked, "removed": len(remove), "need_upload": [h for h in need if h not in pending],
                "processing": [h for h in need if h in pending]}
