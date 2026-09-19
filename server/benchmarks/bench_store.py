"""End-to-end object store: write/read latency healthy vs degraded vs during rebuild (TDD §12.4)."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from cloudstore.config import Config
from cloudstore.db.database import Database
from cloudstore.storage.disks import DiskManager
from cloudstore.storage.objectstore import ObjectStore
from cloudstore.storage.rebuild import RebuildManager

from .common import MB, percentiles, table


def _env(root: Path) -> tuple[ObjectStore, DiskManager, Config]:
    cfg = Config()
    cfg.data_dir = root / "data"
    cfg.fsync = False  # measures the code path, not the laptop SSD's flush latency
    cfg.stripe_cache_bytes = 0  # every read hits the shards
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    dm = DiskManager(db)
    for i in range(6):
        dm.add(root / f"disk{i}", f"d{i}")
    return ObjectStore(db, dm, cfg), dm, cfg


def run(n_objects: int = 60, size: int = 3_000_000) -> str:
    root = Path(tempfile.mkdtemp(prefix="csbench-"))
    try:
        store, dm, cfg = _env(root)
        payloads = {f"media/{i}": os.urandom(size) for i in range(n_objects)}
        t0 = time.perf_counter()
        for k, v in payloads.items():
            store.put_bytes(k, v)
        put_s = time.perf_counter() - t0

        def read_all() -> list[float]:
            lat = []
            for k in payloads:
                t = time.perf_counter()
                store.get(k)
                lat.append((time.perf_counter() - t) * 1000)
            return lat

        read_all()  # warm-up: first-use costs (codec tables, SQLite statement cache)
        healthy = read_all()
        shutil.rmtree(root / "disk0")
        dm.check_all()
        one_down = read_all()
        shutil.rmtree(root / "disk3")
        dm.check_all()
        two_down = read_all()

        # rebuild disk0 onto a new disk while reading
        rb = RebuildManager(store, dm, rate_bytes=0)
        tid = rb.replace_disk(dm.list()[0].id, root / "disk-new", "new")
        during: list[float] = []
        t_rb = threading.Thread(target=rb.run_task, args=(tid,))
        t0 = time.perf_counter()
        t_rb.start()
        while t_rb.is_alive():
            during += read_all()[:10]
        t_rb.join()
        rebuild_s = time.perf_counter() - t0
        st = rb.status(tid)
        rows = []
        for name, lat in [("healthy (6/6)", healthy), ("1 disk failed", one_down), ("2 disks failed", two_down),
                          ("2 failed + rebuild running", during)]:
            p50, p95, p99 = percentiles(lat)
            rows.append([name, len(lat), p50, p95, p99, size / (p50 / 1000) / MB])
        md = table(["state", "reads", "p50 ms", "p95 ms", "p99 ms", "MB/s @p50"], rows)
        md += (f"\n\nWrite: {n_objects} × {size / MB:.0f} MB objects in {put_s:.2f} s "
               f"= **{n_objects * size / put_s / MB:,.0f} MB/s** (4+2 encode + 6 shard files each, fsync off). "
               f"Rebuild of the failed disk ({st['done_shards']} shards, {st['bytes_written'] / MB:,.0f} MB) "
               f"took {rebuild_s:.2f} s with concurrent reads "
               f"(**{st['bytes_written'] / rebuild_s / MB:,.0f} MB/s** of rebuilt shards, unthrottled).")
        return md
    finally:
        shutil.rmtree(root, ignore_errors=True)
