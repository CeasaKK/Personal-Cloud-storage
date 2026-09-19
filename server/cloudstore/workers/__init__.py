"""Background workers (TDD §2): ingest, disk health, scrub/repair, rebuild,
batch sealing + compaction, and maintenance (trash purge, upload expiry)."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..services import Services

log = logging.getLogger(__name__)


class Workers:
    def __init__(self, svc: "Services") -> None:
        self.svc = svc
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.scrub_now = threading.Event()

    def start(self) -> None:
        cfg = self.svc.config
        self._spawn("ingest", self._ingest_loop)
        self._spawn("health", self._every(cfg.health_interval_s, self._health))
        self._spawn("scrub", self._scrub_loop)
        self._spawn("rebuild", self._every(10, self._rebuild))
        self._spawn("batches", self._every(60, self._batches))
        self._spawn("maintenance", self._every(3600, self._maintenance))

    def stop(self) -> None:
        self.stop_event.set()
        self.svc.ingest.wakeup.set()
        self.scrub_now.set()
        for t in self.threads:
            t.join(timeout=10)

    def _spawn(self, name: str, fn: Callable[[], None]) -> None:
        t = threading.Thread(target=self._guard(name, fn), name=f"cloudstore-{name}", daemon=True)
        t.start()
        self.threads.append(t)

    def _guard(self, name: str, fn: Callable[[], None]) -> Callable[[], None]:
        def run():
            while not self.stop_event.is_set():
                try:
                    fn()
                    return
                except Exception:
                    log.exception("worker %s crashed; restarting in 5s", name)
                    self.stop_event.wait(5)
        return run

    def _every(self, interval: float, fn: Callable[[], None]) -> Callable[[], None]:
        def loop():
            while not self.stop_event.is_set():
                try:
                    fn()
                except Exception:
                    log.exception("periodic task %s failed", fn.__name__)
                self.stop_event.wait(interval)
        return loop

    # ------------------------------------------------------------------ tasks
    def _ingest_loop(self) -> None:
        ing = self.svc.ingest
        while not self.stop_event.is_set():
            n = ing.run_pending()
            if n == 0:
                ing.wakeup.wait(5)
                ing.wakeup.clear()

    def _health(self) -> None:
        self.svc.disks.check_all(with_smart=True)

    def _scrub_loop(self) -> None:
        # continuous, rate-limited background progress through the scrub cycle
        while not self.stop_event.is_set():
            full = self.scrub_now.is_set()
            self.scrub_now.clear()
            self.svc.scrubber.run(full=full, max_stripes=None if full else 2000, stop=self.stop_event)
            self.scrub_now.wait(300)

    def _rebuild(self) -> None:
        for tid in self.svc.rebuild.pending_tasks():
            self.svc.rebuild.run_task(tid, stop=self.stop_event)

    def _batches(self) -> None:
        for bid in self.svc.batches.due_for_seal():
            self.svc.batches.seal(bid)
        self.svc.batches.compact()

    def _maintenance(self) -> None:
        purged = self.svc.files.purge_expired_trash()
        now = time.time()
        expired = self.svc.db.all("SELECT id FROM uploads WHERE completed_at IS NULL AND expires_at < ?", (now,))
        for r in expired:
            (self.svc.config.staging_dir / "uploads" / f"{r['id']}.part").unlink(missing_ok=True)
            self.svc.db.execute("DELETE FROM uploads WHERE id = ?", (r["id"],))
        self.svc.db.execute("DELETE FROM uploads WHERE completed_at IS NOT NULL AND completed_at < ?", (now - 86400,))
        self.svc.db.execute("DELETE FROM refresh_tokens WHERE expires_at < ?", (now,))
        self.svc.batches.save_bloom()
        self._snapshot_if_due()
        if purged or expired:
            log.info("maintenance: purged %d trashed files, expired %d uploads", purged, len(expired))

    def _snapshot_if_due(self) -> None:
        from ..storage.snapshots import PREFIX, latest_snapshot, take_snapshot

        latest = latest_snapshot(self.svc.db)
        age = time.time() - int(latest[len(PREFIX):]) if latest else float("inf")
        if age >= 86400 and self.svc.disks.writable():
            take_snapshot(self.svc.db, self.svc.store)
