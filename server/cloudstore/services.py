"""Service container: wires storage, indexes, pipeline and workers together."""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Iterator

from .cache.lru import SizeAwareLRU
from .config import Config
from .db.database import Database
from .dedup.batchstore import BatchStore
from .ingest.pipeline import IngestPipeline
from .ingest.thumbnails import VARIANTS, derived_path, open_oriented, placeholder, video_frame, write_derivatives
from .phash.duplicates import DuplicateIndex
from .phash.hashing import to_unsigned
from .spatial.quadtree import QuadTree
from .storage.disks import DiskManager
from .storage.objectstore import ObjectStore
from .storage.rebuild import RebuildManager
from .storage.scrub import Scrubber
from .sync.manager import SyncManager

log = logging.getLogger(__name__)


class ThumbnailService:
    def __init__(self, svc: "Services") -> None:
        self.svc = svc
        self.root = svc.config.derived_dir
        self.cache: SizeAwareLRU[tuple[int, str]] = SizeAwareLRU(svc.config.thumb_cache_bytes)

    def record(self, file_id: int, sha256: str) -> None:
        now = time.time()
        for variant in VARIANTS:
            p = derived_path(self.root, variant, sha256)
            if p.exists():
                self.svc.db.execute(
                    "INSERT OR REPLACE INTO thumbnails(file_id, variant, path, bytes, created_at) VALUES(?,?,?,?,?)",
                    (file_id, variant, str(p), p.stat().st_size, now))

    def get(self, file_id: int, variant: str) -> tuple[bytes, str]:
        mime = VARIANTS[variant][1]
        cached = self.cache.get((file_id, variant))
        if cached is not None:
            return cached, mime
        row = self.svc.db.one("SELECT sha256, kind, tier FROM files WHERE id = ?", (file_id,))
        if row is None:
            raise KeyError(file_id)
        p = derived_path(self.root, variant, row["sha256"])
        if not p.exists():
            self.regenerate(file_id)
        data = p.read_bytes()
        self.cache.put((file_id, variant), data)
        return data, mime

    def regenerate(self, file_id: int) -> None:
        """Rebuild derivatives from the original (after index recovery or SSD loss)."""
        f = self.svc.db.one("SELECT * FROM files WHERE id = ?", (file_id,))
        with tempfile.NamedTemporaryFile(dir=self.svc.config.staging_dir, delete=True) as tmp:
            for part in self.svc.files.iter_bytes(f, 0, f["size"]):
                tmp.write(part)
            tmp.flush()
            img = None
            try:
                if f["kind"] == "image" and f["tier"] == "media":
                    img = open_oriented(Path(tmp.name))
                elif f["kind"] == "video":
                    img = video_frame(Path(tmp.name))
            except Exception:
                img = None
            write_derivatives(img or placeholder(f["kind"]), self.root, f["sha256"],
                              self.svc.config.thumb_size, self.svc.config.proxy_size)
        self.record(file_id, f["sha256"])


class FileService:
    def __init__(self, svc: "Services") -> None:
        self.svc = svc
        self.db = svc.db

    def get(self, file_id: int):
        return self.db.one("SELECT * FROM files WHERE id = ?", (file_id,))

    def iter_bytes(self, f, start: int, end: int) -> Iterator[bytes]:
        """Original bytes [start, end) of a file from whichever tier holds it."""
        if f["purged_at"] is not None:
            raise FileNotFoundError("file was permanently deleted")
        if f["tier"] == "media":
            yield from self.svc.store.iter_range(f"media/{f['sha256']}", start, end)
            return
        pos = 0
        rows = self.db.all("SELECT fc.chunk_hash, c.size FROM file_chunks fc JOIN chunks c ON c.hash = fc.chunk_hash "
                           "WHERE fc.file_id = ? ORDER BY fc.seq", (f["id"],))
        for r in rows:
            c0, c1 = pos, pos + r["size"]
            pos = c1
            if c1 <= start:
                continue
            if c0 >= end:
                break
            data = self.svc.batches.read_chunk(bytes(r["chunk_hash"]))
            yield data[max(start, c0) - c0 : min(end, c1) - c0]

    def trash(self, ids: list[int]) -> int:
        now = time.time()
        n = 0
        for fid in ids:
            f = self.get(fid)
            if f is None or f["trashed_at"] is not None:
                continue
            self.db.execute("UPDATE files SET trashed_at = ? WHERE id = ?", (now, fid))
            self.svc.dups.remove(fid, f["phash"])
            if f["lat"] is not None:
                self.svc.geo.remove(f["lat"], f["lon"], fid)
            n += 1
        return n

    def restore(self, ids: list[int]) -> int:
        n = 0
        for fid in ids:
            f = self.get(fid)
            if f is None or f["trashed_at"] is None or f["purged_at"] is not None:
                continue
            self.db.execute("UPDATE files SET trashed_at = NULL WHERE id = ?", (fid,))
            if f["phash"] is not None:
                self.svc.dups.add(fid, to_unsigned(f["phash"]))
            if f["lat"] is not None:
                self.svc.geo.insert(f["lat"], f["lon"], fid, f["capture_ts"])
            n += 1
        return n

    def purge(self, ids: list[int]) -> int:
        """Permanently delete bytes; keep a tombstone row so sync never re-uploads it."""
        n = 0
        for fid in ids:
            f = self.get(fid)
            if f is None or f["purged_at"] is not None:
                continue
            # tombstone first (drops the FK to the object), then free the bytes
            with self.db.tx() as c:
                if f["tier"] == "nonmedia":
                    self.svc.batches.release_file(fid, c)
                c.execute("DELETE FROM thumbnails WHERE file_id = ?", (fid,))
                c.execute("DELETE FROM album_items WHERE file_id = ?", (fid,))
                c.execute("UPDATE files SET purged_at = ?, trashed_at = COALESCE(trashed_at, ?), object_id = NULL, "
                          "dup_group = NULL, favorite = 0 WHERE id = ?", (time.time(), time.time(), fid))
            if f["tier"] == "media":
                self.svc.store.delete(f"media/{f['sha256']}")
            for variant in VARIANTS:
                derived_path(self.svc.config.derived_dir, variant, f["sha256"]).unlink(missing_ok=True)
            n += 1
        return n

    def purge_expired_trash(self) -> int:
        cutoff = time.time() - self.svc.config.trash_retention_days * 86400
        ids = [r["id"] for r in self.db.all(
            "SELECT id FROM files WHERE trashed_at IS NOT NULL AND trashed_at < ? AND purged_at IS NULL", (cutoff,))]
        return self.purge(ids)


class Services:
    def __init__(self, config: Config) -> None:
        config.ensure_dirs()
        self.config = config
        self.db = Database(config.db_path)
        self.disks = DiskManager(self.db, config.disk_error_threshold)
        self.store = ObjectStore(self.db, self.disks, config)
        self.batches = BatchStore(self.db, self.store, config)
        self.sync = SyncManager(self.db)
        self.dups = DuplicateIndex(self.db, config.dup_radius)
        self.geo = QuadTree()
        self.files = FileService(self)
        self.thumbs = ThumbnailService(self)
        self.ingest = IngestPipeline(self)
        self.scrubber = Scrubber(self.store, config.scrub_rate_bytes, config.scrub_interval_days)
        self.rebuild = RebuildManager(self.store, self.disks, config.rebuild_rate_bytes)
        self.workers = None

    def startup(self) -> None:
        recovered = self.store.recover_pending()
        if recovered:
            log.warning("crash recovery removed %d pending stripes/objects", recovered)
        self.disks.check_all()
        self.batches.recover()
        n = self.dups.load()
        rows = self.db.all("SELECT id, lat, lon, capture_ts FROM files WHERE lat IS NOT NULL AND trashed_at IS NULL")
        for r in rows:
            self.geo.insert(r["lat"], r["lon"], r["id"], r["capture_ts"])
        log.info("indexes loaded: %d perceptual hashes, %d geotagged files", n, len(rows))
        if self.config.enable_workers:
            from .workers import Workers

            self.workers = Workers(self)
            self.workers.start()

    def shutdown(self) -> None:
        if self.workers is not None:
            self.workers.stop()
        self.batches.save_bloom()
