"""Durable ingest pipeline (TDD §5).

Jobs live in ``ingest_jobs`` so a crash mid-ingest is retried on restart. Steps:
verify SHA-256 → classify → metadata → derivatives → perceptual hash →
durable store (erasure-coded object or non-media batch) → files row →
indexes (duplicates, map) → device manifest → remove staging file.
Every step is idempotent (the object store and batch store key on content).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

from ..phash.hashing import dhash, phash, sharpness, to_signed
from ..spatial import geohash
from . import metadata as md
from .classify import classify
from .thumbnails import open_oriented, placeholder, video_frame, write_derivatives

if TYPE_CHECKING:
    from ..services import Services

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(4 * 1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class IngestPipeline:
    def __init__(self, svc: "Services") -> None:
        self.svc = svc
        self.db = svc.db
        self.config = svc.config
        self.wakeup = threading.Event()
        self._lock = threading.Lock()

    def enqueue(self, staging_path: Path, sha256: str, meta: dict) -> int:
        now = time.time()
        job = self.db.execute(
            "INSERT INTO ingest_jobs(staging_path, sha256, metadata, state, created_at, updated_at) "
            "VALUES(?,?,?, 'queued', ?, ?)", (str(staging_path), sha256, json.dumps(meta), now, now)).lastrowid
        self.wakeup.set()
        return job

    def run_pending(self, limit: int = 50) -> int:
        """Process queued jobs (one worker at a time). Returns jobs processed."""
        if not self._lock.acquire(blocking=False):
            return 0
        try:
            # a job left 'running' by a crash goes back to the queue
            self.db.execute("UPDATE ingest_jobs SET state = 'queued' WHERE state = 'running'")
            rows = self.db.all("SELECT * FROM ingest_jobs WHERE state = 'queued' ORDER BY id LIMIT ?", (limit,))
            for job in rows:
                self._run(job)
            return len(rows)
        finally:
            self._lock.release()

    def _run(self, job) -> None:
        jid = job["id"]
        self.db.execute("UPDATE ingest_jobs SET state='running', attempts=attempts+1, updated_at=? WHERE id=?",
                        (time.time(), jid))
        try:
            file_id = self.process(Path(job["staging_path"]), job["sha256"], json.loads(job["metadata"]))
            self.db.execute("UPDATE ingest_jobs SET state='done', file_id=?, error=NULL, updated_at=? WHERE id=?",
                            (file_id, time.time(), jid))
        except Exception as e:
            log.exception("ingest job %d failed", jid)
            state = "failed" if job["attempts"] + 1 >= MAX_ATTEMPTS else "queued"
            self.db.execute("UPDATE ingest_jobs SET state=?, error=?, updated_at=? WHERE id=?",
                            (state, f"{type(e).__name__}: {e}"[:1000], time.time(), jid))

    # ------------------------------------------------------------------ steps
    def process(self, path: Path, sha256: str, meta: dict) -> int:
        svc = self.svc
        device_id = meta.get("device_id")
        existing = self.db.one("SELECT id FROM files WHERE sha256 = ?", (sha256,))
        if existing is not None:
            # content already known (possibly tombstoned): dedup, just link the device
            if device_id:
                svc.sync.link(device_id, [sha256])
            path.unlink(missing_ok=True)
            return existing["id"]
        if not path.exists():
            raise FileNotFoundError(f"staging file missing: {path}")
        actual = sha256_file(path)
        if actual != sha256:
            path.unlink(missing_ok=True)
            raise ValueError(f"sha256 mismatch: declared {sha256}, got {actual}")

        filename = meta.get("filename") or sha256
        with open(path, "rb") as f:
            head = f.read(512)
        cls = classify(head, filename)
        size = path.stat().st_size
        now = time.time()
        info: dict = {}
        img: Image.Image | None = None
        if cls.kind == "image" and cls.tier == "media":
            try:
                raw = Image.open(path)
                info = md.image_metadata(raw)
                img = open_oriented(path)
            except Exception as e:  # undecodable image: store it anyway
                log.warning("could not decode image %s: %s", filename, e)
        elif cls.kind == "video":
            info = md.video_metadata(path)
            img = video_frame(path)
        client_ts = _float(meta.get("created_at"))
        capture_ts = md.choose_capture_ts(info, client_ts, now)

        fields = {
            "sha256": sha256, "size": size, "mime": cls.mime, "kind": cls.kind, "tier": cls.tier,
            "filename": filename, "capture_ts": capture_ts, "upload_ts": now,
            "width": info.get("width"), "height": info.get("height"), "duration": info.get("duration"),
            "camera_make": info.get("camera_make"), "camera_model": info.get("camera_model"),
            "lat": info.get("lat"), "lon": info.get("lon"),
            "geohash": geohash.encode(info["lat"], info["lon"], 9) if info.get("lat") is not None else None,
            "exif_json": json.dumps(info.get("exif")) if info.get("exif") else None,
        }
        ph = None
        if img is not None and cls.kind == "image":
            ph = phash(img)
            fields.update(phash=to_signed(ph), dhash=to_signed(dhash(img)), sharpness=sharpness(img))

        # derivatives before the durable write so the UI never shows a file without a thumbnail
        if cls.tier == "media":
            source = img or placeholder(cls.kind)
            write_derivatives(source, self.config.derived_dir, sha256, self.config.thumb_size, self.config.proxy_size)

        def insert(conn) -> int:
            cols = ", ".join(fields)
            return conn.execute(f"INSERT INTO files({cols}) VALUES({', '.join('?' * len(fields))})",
                                tuple(fields.values())).lastrowid

        if cls.tier == "media":
            obj_id = svc.store.put_file(f"media/{sha256}", path)
            fields["object_id"] = obj_id
            with self.db.tx() as c:
                file_id = insert(c)
        else:
            file_id = svc.batches.ingest_file(path, insert)["file_id"]

        svc.thumbs.record(file_id, sha256)
        if ph is not None:
            svc.dups.add(file_id, ph)
        if fields["lat"] is not None:
            svc.geo.insert(fields["lat"], fields["lon"], file_id, capture_ts)
        if device_id:
            svc.sync.link(device_id, [sha256])
        path.unlink(missing_ok=True)
        log.info("ingested %s (%s, %s, %d bytes) as file %d", filename, cls.mime, cls.tier, size, file_id)
        return file_id


def _float(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
