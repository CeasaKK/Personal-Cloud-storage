"""Background scrub: verify every shard checksum, repair from survivors (TDD §4.9)."""

from __future__ import annotations

import logging
import threading
import time

from .objectstore import ObjectStore
from .throttle import TokenBucket

log = logging.getLogger(__name__)


class Scrubber:
    def __init__(self, store: ObjectStore, rate_bytes: int, interval_days: float) -> None:
        self.store = store
        self.db = store.db
        self.bucket = TokenBucket(rate_bytes, burst=rate_bytes)
        self.interval_s = interval_days * 86400

    def run(self, full: bool = False, max_stripes: int | None = None,
            stop: threading.Event | None = None) -> dict:
        """One scrub pass.

        full=True verifies every stripe not verified since this run started;
        otherwise only stripes whose last scrub is older than the interval.
        Repair of stripes already known to have bad shards is done first.
        """
        started = time.time()
        cutoff = started if full else started - self.interval_s
        run_id = self.db.execute(
            "INSERT INTO scrub_runs(started_at, status) VALUES(?, 'running')", (started,)
        ).lastrowid
        stats = dict(stripes_checked=0, shards_verified=0, bytes_read=0, corrupt_found=0,
                     missing_found=0, repaired=0, unrecoverable=0)
        status = "done"

        def process(stripe_id: str) -> None:
            res = self.store.repair_stripe(stripe_id)
            stats["stripes_checked"] += 1
            stats["shards_verified"] += res.verified
            stats["bytes_read"] += res.bytes_read
            stats["corrupt_found"] += res.corrupt
            stats["missing_found"] += res.missing
            stats["repaired"] += res.repaired
            stats["unrecoverable"] += int(res.unrecoverable)
            self.bucket.consume(res.bytes_read + res.bytes_written)

        try:
            for sid in self.store.stripes_needing_repair(limit=10_000):
                if stop is not None and stop.is_set():
                    status = "aborted"
                    break
                process(sid)
            while status == "done":
                batch = self.db.all(
                    "SELECT id FROM stripes WHERE state IN ('committed','unrecoverable') "
                    "AND COALESCE(last_scrubbed_at, 0) < ? ORDER BY COALESCE(last_scrubbed_at, 0), id LIMIT 200",
                    (cutoff,),
                )
                if not batch:
                    break
                progressed = False
                for r in batch:
                    if stop is not None and stop.is_set():
                        status = "aborted"
                        break
                    if max_stripes is not None and stats["stripes_checked"] >= max_stripes:
                        status = "done"
                        batch = []
                        break
                    before = self.db.scalar("SELECT last_scrubbed_at FROM stripes WHERE id = ?", (r["id"],))
                    process(r["id"])
                    after = self.db.scalar("SELECT last_scrubbed_at FROM stripes WHERE id = ?", (r["id"],))
                    progressed = progressed or after != before
                self._save(run_id, stats, "running")
                if not batch or not progressed:
                    break
        except Exception:
            log.exception("scrub run failed")
            status = "aborted"
        self._save(run_id, stats, status, finished=time.time())
        log.info("scrub %s: %s", status, stats)
        return {"id": run_id, "status": status, **stats}

    def _save(self, run_id: int, stats: dict, status: str, finished: float | None = None) -> None:
        self.db.execute(
            "UPDATE scrub_runs SET status=?, finished_at=?, stripes_checked=?, shards_verified=?, bytes_read=?, "
            "corrupt_found=?, missing_found=?, repaired=?, unrecoverable=? WHERE id=?",
            (status, finished, stats["stripes_checked"], stats["shards_verified"], stats["bytes_read"],
             stats["corrupt_found"], stats["missing_found"], stats["repaired"], stats["unrecoverable"], run_id),
        )
