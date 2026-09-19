"""Disk replacement and background rebuild (TDD §4.10).

``replace_disk`` registers the new disk and queues a task; ``run_task``
reconstructs every shard that lived on the old disk onto the remaining disks
(the new, empty disk wins placement because it has the most free space). The
task runs in its own thread and is throttled, so foreground reads (degraded path)
and writes continue meanwhile.
"""

from __future__ import annotations

import logging
import threading
import time

from .disks import FAILED, RETIRED, DiskManager
from .objectstore import ObjectStore
from .throttle import TokenBucket

log = logging.getLogger(__name__)


class RebuildManager:
    def __init__(self, store: ObjectStore, disks: DiskManager, rate_bytes: int) -> None:
        self.store = store
        self.disks = disks
        self.db = store.db
        self.bucket = TokenBucket(rate_bytes, burst=rate_bytes)

    def replace_disk(self, old_id: int, new_path: str, label: str | None = None) -> int:
        old = self.disks.get(old_id)
        new = self.disks.add(new_path, label)
        return self._queue(old.id, new.id)

    def evacuate(self, disk_id: int) -> int:
        """Move all shards off a disk without a dedicated replacement."""
        return self._queue(disk_id, None)

    def _queue(self, source_id: int, target_id: int | None) -> int:
        total = self.db.scalar("SELECT COUNT(*) FROM shards WHERE disk_id = ?", (source_id,))
        return self.db.execute(
            "INSERT INTO rebuild_tasks(source_disk_id, target_disk_id, status, total_shards) VALUES(?,?, 'queued', ?)",
            (source_id, target_id, total),
        ).lastrowid

    def pending_tasks(self) -> list[int]:
        return [r["id"] for r in self.db.all(
            "SELECT id FROM rebuild_tasks WHERE status IN ('queued','running') ORDER BY id")]

    def run_task(self, task_id: int, stop: threading.Event | None = None) -> dict:
        task = self.db.one("SELECT * FROM rebuild_tasks WHERE id = ?", (task_id,))
        source = task["source_disk_id"]
        self.db.execute("UPDATE rebuild_tasks SET status='running', started_at=COALESCE(started_at, ?) WHERE id=?",
                        (time.time(), task_id))
        if task["target_disk_id"] is not None:
            self.disks.set_status(task["target_disk_id"], "rebuilding")
        done = failed = written = 0
        try:
            while True:
                if stop is not None and stop.is_set():
                    self.db.execute("UPDATE rebuild_tasks SET status='queued' WHERE id=?", (task_id,))
                    return self.status(task_id)
                ids = [r[0] for r in self.db.all(
                    "SELECT DISTINCT stripe_id FROM shards WHERE disk_id = ? LIMIT 100", (source,))]
                if not ids:
                    break
                progressed = False
                for sid in ids:
                    before = self.db.scalar("SELECT COUNT(*) FROM shards WHERE stripe_id=? AND disk_id=?", (sid, source))
                    res = self.store.repair_stripe(sid, force_move_from={source})
                    after = self.db.scalar("SELECT COUNT(*) FROM shards WHERE stripe_id=? AND disk_id=?", (sid, source))
                    moved = before - after
                    done += moved
                    written += res.bytes_written
                    if moved == 0:
                        failed += before
                        # detach unrecoverable/unplaceable shards so the loop terminates
                        self.db.execute("UPDATE shards SET disk_id = NULL, state = 'missing' "
                                        "WHERE stripe_id = ? AND disk_id = ?", (sid, source))
                    else:
                        progressed = True
                    self.bucket.consume(res.bytes_read + res.bytes_written)
                    self.db.execute(
                        "UPDATE rebuild_tasks SET done_shards=?, failed_shards=?, bytes_written=? WHERE id=?",
                        (done, failed, written, task_id))
                if not progressed and not ids:
                    break
            self.db.execute(
                "UPDATE rebuild_tasks SET status=?, finished_at=?, done_shards=?, failed_shards=?, bytes_written=? "
                "WHERE id=?", ("done" if failed == 0 else "failed", time.time(), done, failed, written, task_id))
            self.disks.set_status(source, RETIRED)
            if task["target_disk_id"] is not None:
                self.disks.set_status(task["target_disk_id"], "online")
            log.info("rebuild task %d finished: %d shards moved, %d failed", task_id, done, failed)
        except Exception as e:
            log.exception("rebuild task %d failed", task_id)
            self.db.execute("UPDATE rebuild_tasks SET status='failed', error=?, finished_at=? WHERE id=?",
                            (str(e), time.time(), task_id))
        return self.status(task_id)

    def status(self, task_id: int) -> dict:
        r = self.db.one("SELECT * FROM rebuild_tasks WHERE id = ?", (task_id,))
        d = dict(r)
        elapsed = (d["finished_at"] or time.time()) - (d["started_at"] or time.time())
        d["rate_bytes_s"] = d["bytes_written"] / elapsed if elapsed > 0 else 0
        remaining = d["total_shards"] - d["done_shards"] - d["failed_shards"]
        per_shard = elapsed / d["done_shards"] if d["done_shards"] else None
        d["eta_s"] = remaining * per_shard if per_shard and d["status"] == "running" else None
        return d


def auto_evacuate_failed(disks: DiskManager, rebuild: RebuildManager) -> None:
    """Hook for operators who want immediate re-protection onto spare capacity
    when a disk fails (only useful with more than k+m disks)."""
    for d in disks.list((FAILED,)):
        if not rebuild.db.scalar("SELECT 1 FROM rebuild_tasks WHERE source_disk_id=? AND status IN ('queued','running')",
                                 (d.id,)):
            rebuild.evacuate(d.id)
