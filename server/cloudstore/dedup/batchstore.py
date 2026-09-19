"""Non-media tier: CDC dedup + batched zstd compression (TDD §6).

Write path (``ingest_file``): Rabin CDC → per-chunk BLAKE2b id → Bloom filter
→ (maybe) chunk index → new chunks appended to the *open batch* staging file on
the SSD → chunk/file_chunks rows committed in the caller's transaction.

Seal (``seal``): when the open batch reaches the threshold (1 GiB) or its oldest
data is older than the max age (6 h), its live chunks are laid out grouped by
file type, compressed as seekable ~4 MiB zstd frames with a per-batch trained
dictionary, and stored as one erasure-coded object:

    [dictionary][frame 0][frame 1]…[frame index JSON][u64 index offset][u64 index len]["CSBATCH1"]

Read (``read_chunk``): chunk → (batch, raw offset) → frame → byte-range read of
just that frame from the object store → decompress → slice.

GC (``compact``): chunks whose refcount reaches 0 are dead; a sealed batch whose
live ratio drops below 50 % has its live chunks re-appended to the open batch and
the old object deleted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import struct
import threading
import time
from collections import OrderedDict
from pathlib import Path, PurePath
from typing import Callable, Iterator

import zstandard as zstd

from ..config import Config
from ..db.database import Database
from ..storage.objectstore import ObjectNotFound, ObjectStore
from .bloom import BloomFilter
from .rabin import Chunker

log = logging.getLogger(__name__)

FOOTER = struct.Struct("<QQ8s")
FOOTER_MAGIC = b"CSBATCH1"
DICT_SIZE = 112 * 1024


def chunk_id(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


class BatchStore:
    def __init__(self, db: Database, store: ObjectStore, config: Config) -> None:
        self.db = db
        self.store = store
        self.config = config
        self.chunker = Chunker(config.cdc_min, config.cdc_avg_bits, config.cdc_max)
        self.dir = config.staging_dir / "batches"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.bloom_path = config.data_dir / "chunks.bloom"
        self._lock = threading.RLock()
        self._dicts: OrderedDict[int, zstd.ZstdCompressionDict | None] = OrderedDict()
        self._frames: OrderedDict[tuple[int, int], bytes] = OrderedDict()
        self.bloom = self._load_bloom()

    # ------------------------------------------------------------------ bloom
    def _load_bloom(self) -> BloomFilter:
        n = int(self.db.scalar("SELECT COUNT(*) FROM chunks"))
        bf = BloomFilter.load(self.bloom_path)
        if bf is not None and bf.count == n:
            return bf
        bf = BloomFilter(self.config.bloom_capacity, self.config.bloom_fpr)
        for (h,) in self.db.conn.execute("SELECT hash FROM chunks"):
            bf.add(bytes(h))
        log.info("rebuilt chunk bloom filter from index (%d chunks)", n)
        return bf

    def save_bloom(self) -> None:
        with self._lock:
            self.bloom.save(self.bloom_path)

    # ------------------------------------------------------------------ open batch
    def _raw_path(self, batch_id: int) -> Path:
        return self.dir / f"{batch_id}.raw"

    def _open_batch(self) -> int:
        bid = self.db.scalar("SELECT id FROM batches WHERE state = 'open' ORDER BY id LIMIT 1")
        if bid is None:
            bid = self.db.execute("INSERT INTO batches(state, created_at) VALUES('open', ?)", (time.time(),)).lastrowid
        return int(bid)

    def _lookup(self, h: bytes):
        if not self.bloom.might_contain(h):
            return None
        row = self.db.one("SELECT hash, size, refcount FROM chunks WHERE hash = ?", (h,))
        if row is None:
            self.bloom.stats["false_positive"] += 1
        return row

    def ingest_file(self, path: Path, commit: Callable[["sqlite3.Connection"], int]) -> dict:
        """Chunk + dedup a file. ``commit(conn)`` inserts the files row inside the
        same transaction that records chunk references and returns its id."""
        with self._lock:
            bid = self._open_batch()
            raw = self._raw_path(bid)
            pieces: list[tuple[bytes, int]] = []
            new: dict[bytes, tuple[int, int]] = {}  # hash -> (offset, size)
            stats = {"chunks": 0, "new_chunks": 0, "bytes": 0, "new_bytes": 0}
            with open(raw, "ab") as out:
                offset = out.tell()
                with open(path, "rb") as f:
                    for data in self.chunker.chunks(f):
                        h = chunk_id(data)
                        pieces.append((h, len(data)))
                        stats["chunks"] += 1
                        stats["bytes"] += len(data)
                        if h in new or self._lookup(h) is not None:
                            continue
                        out.write(data)
                        new[h] = (offset, len(data))
                        offset += len(data)
                        stats["new_chunks"] += 1
                        stats["new_bytes"] += len(data)
                out.flush()
                if self.config.fsync:
                    os.fsync(out.fileno())
            now = time.time()
            with self.db.tx() as c:
                file_id = commit(c)
                for h, (off, size) in new.items():
                    c.execute("INSERT INTO chunks(hash, size, batch_id, raw_offset, refcount) VALUES(?,?,?,?,0)",
                              (h, size, bid, off))
                live_delta: dict[int, int] = {}
                for seq, (h, size) in enumerate(pieces):
                    row = c.execute("SELECT batch_id, refcount FROM chunks WHERE hash = ?", (h,)).fetchone()
                    if row["refcount"] == 0:
                        live_delta[row["batch_id"]] = live_delta.get(row["batch_id"], 0) + size
                    c.execute("UPDATE chunks SET refcount = refcount + 1 WHERE hash = ?", (h,))
                    c.execute("INSERT INTO file_chunks(file_id, seq, chunk_hash) VALUES(?,?,?)", (file_id, seq, h))
                c.execute(
                    "UPDATE batches SET raw_bytes = raw_bytes + ?, first_data_at = COALESCE(first_data_at, ?) "
                    "WHERE id = ?", (stats["new_bytes"], now if new else None, bid))
                for b, delta in live_delta.items():
                    c.execute("UPDATE batches SET live_bytes = live_bytes + ? WHERE id = ?", (delta, b))
            for h in new:
                self.bloom.add(h)
            stats["file_id"] = file_id
            return stats

    # ------------------------------------------------------------------ read
    def read_file(self, file_id: int) -> Iterator[bytes]:
        for r in self.db.all("SELECT chunk_hash FROM file_chunks WHERE file_id = ? ORDER BY seq", (file_id,)):
            yield self.read_chunk(bytes(r["chunk_hash"]))

    def read_chunk(self, h: bytes) -> bytes:
        for attempt in range(2):
            row = self.db.one("SELECT c.size, c.raw_offset, c.batch_id, b.state FROM chunks c "
                              "JOIN batches b ON b.id = c.batch_id WHERE c.hash = ?", (h,))
            if row is None:
                raise KeyError(h.hex())
            try:
                if row["state"] in ("open", "sealing"):
                    with open(self._raw_path(row["batch_id"]), "rb") as f:
                        f.seek(row["raw_offset"])
                        data = f.read(row["size"])
                else:
                    data = self._read_sealed(row["batch_id"], row["raw_offset"], row["size"])
            except (FileNotFoundError, ObjectNotFound):
                if attempt == 0:
                    continue  # moved concurrently by seal/compaction; re-resolve
                raise
            if chunk_id(data) != h:
                raise IOError(f"chunk {h.hex()} failed verification")
            return data
        raise KeyError(h.hex())

    def _dictionary(self, batch_id: int) -> zstd.ZstdCompressionDict | None:
        if batch_id in self._dicts:
            return self._dicts[batch_id]
        dlen = self.db.scalar("SELECT dict_len FROM batches WHERE id = ?", (batch_id,)) or 0
        d = zstd.ZstdCompressionDict(self.store.read_range(f"batch/{batch_id}", 0, dlen)) if dlen else None
        self._dicts[batch_id] = d
        if len(self._dicts) > 32:
            self._dicts.popitem(last=False)
        return d

    def _read_sealed(self, batch_id: int, raw_offset: int, size: int) -> bytes:
        fr = self.db.one(
            "SELECT idx, raw_start, raw_len, comp_offset, comp_len FROM batch_frames "
            "WHERE batch_id = ? AND raw_start <= ? ORDER BY raw_start DESC LIMIT 1", (batch_id, raw_offset))
        key = (batch_id, fr["idx"])
        frame = self._frames.get(key)
        if frame is None:
            comp = self.store.read_range(f"batch/{batch_id}", fr["comp_offset"], fr["comp_len"])
            d = self._dictionary(batch_id)
            dctx = zstd.ZstdDecompressor(dict_data=d) if d else zstd.ZstdDecompressor()
            frame = dctx.decompress(comp, max_output_size=fr["raw_len"])
            self._frames[key] = frame
            if len(self._frames) > 16:
                self._frames.popitem(last=False)
        else:
            self._frames.move_to_end(key)
        start = raw_offset - fr["raw_start"]
        return frame[start : start + size]

    # ------------------------------------------------------------------ seal
    def due_for_seal(self) -> list[int]:
        now = time.time()
        rows = self.db.all("SELECT id, raw_bytes, first_data_at FROM batches WHERE state = 'open'")
        return [r["id"] for r in rows if r["raw_bytes"] >= self.config.batch_threshold
                or (r["first_data_at"] and now - r["first_data_at"] >= self.config.batch_max_age_s)]

    def seal(self, batch_id: int) -> dict:
        """Compress an open batch into a seekable, dictionary-compressed object.

        Holds the store lock for the whole seal: refcount-0 chunks are dropped at
        seal time, so an ingest must not resurrect one concurrently.
        """
        with self._lock:
            return self._seal_locked(batch_id)

    def _seal_locked(self, batch_id: int) -> dict:
        state = self.db.scalar("SELECT state FROM batches WHERE id = ?", (batch_id,))
        if state not in ("open", "sealing"):
            return {"batch_id": batch_id, "skipped": state}
        self.db.execute("UPDATE batches SET state = 'sealing' WHERE id = ?", (batch_id,))
        raw_path = self._raw_path(batch_id)
        chunks = self.db.all(
            "SELECT c.hash, c.size, c.raw_offset, c.refcount, "
            "(SELECT f.filename FROM file_chunks fc JOIN files f ON f.id = fc.file_id "
            " WHERE fc.chunk_hash = c.hash LIMIT 1) AS filename "
            "FROM chunks c WHERE c.batch_id = ? AND c.refcount > 0", (batch_id,))
        # group similar content: order by file extension, then original position
        order = sorted(chunks, key=lambda r: (PurePath(r["filename"] or "").suffix.lower(), r["raw_offset"]))
        with open(raw_path, "rb") as f:
            def read(r) -> bytes:
                f.seek(r["raw_offset"])
                return f.read(r["size"])

            samples = [read(r)[:16384] for r in order[:: max(1, len(order) // 2000)]]
            dict_bytes = b""
            if len(samples) >= 8:
                try:
                    dict_bytes = zstd.train_dictionary(DICT_SIZE, samples).as_bytes()
                except zstd.ZstdError:
                    dict_bytes = b""
            cdict = zstd.ZstdCompressionDict(dict_bytes) if dict_bytes else None
            cctx = zstd.ZstdCompressor(level=12, dict_data=cdict) if cdict else zstd.ZstdCompressor(level=12)

            parts = [dict_bytes]
            pos = len(dict_bytes)
            frames = []
            new_offsets: list[tuple[int, bytes]] = []
            frame_buf = bytearray()
            raw_cursor = 0
            frame_start = 0

            def flush():
                nonlocal pos, frame_buf, frame_start
                if not frame_buf:
                    return
                comp = cctx.compress(bytes(frame_buf))
                frames.append((len(frames), frame_start, len(frame_buf), pos, len(comp)))
                parts.append(comp)
                pos += len(comp)
                frame_start += len(frame_buf)
                frame_buf = bytearray()

            for r in order:
                data = read(r)
                if chunk_id(data) != bytes(r["hash"]):
                    raise IOError(f"staging chunk {bytes(r['hash']).hex()} corrupt before seal")
                new_offsets.append((raw_cursor, bytes(r["hash"])))
                frame_buf += data
                raw_cursor += len(data)
                if len(frame_buf) >= self.config.batch_frame_size:
                    flush()
            flush()
        index = json.dumps({"frames": frames, "dict_len": len(dict_bytes)}).encode()
        parts.append(index)
        parts.append(FOOTER.pack(pos, len(index), FOOTER_MAGIC))
        blob = b"".join(parts)
        key = f"batch/{batch_id}"
        self.store.delete(key)  # leftover from an interrupted seal
        obj_id = self.store.put_bytes(key, blob)
        live = sum(r["size"] for r in order)
        with self.db.tx() as c:
            c.execute("DELETE FROM batch_frames WHERE batch_id = ?", (batch_id,))
            c.executemany("INSERT INTO batch_frames(batch_id, idx, raw_start, raw_len, comp_offset, comp_len) "
                          "VALUES(?,?,?,?,?,?)", [(batch_id, *fr) for fr in frames])
            c.executemany("UPDATE chunks SET raw_offset = ? WHERE hash = ?", new_offsets)
            c.execute("DELETE FROM chunks WHERE batch_id = ? AND refcount = 0", (batch_id,))
            c.execute("UPDATE batches SET state='sealed', object_id=?, compressed_bytes=?, frame_count=?, dict_len=?, "
                      "raw_bytes=?, live_bytes=?, sealed_at=? WHERE id=?",
                      (obj_id, len(blob), len(frames), len(dict_bytes), live, live, time.time(), batch_id))
        raw_path.unlink(missing_ok=True)
        self._dicts.pop(batch_id, None)
        self.save_bloom()
        report = {"batch_id": batch_id, "raw_bytes": live, "compressed_bytes": len(blob),
                  "ratio": (live / len(blob)) if blob else 0, "frames": len(frames),
                  "dict_bytes": len(dict_bytes)}
        log.info("sealed batch %s", report)
        return report

    def recover(self) -> None:
        """Startup: a batch interrupted mid-seal goes back to sealing from staging."""
        for r in self.db.all("SELECT id FROM batches WHERE state = 'sealing'"):
            if self._raw_path(r["id"]).exists():
                self.seal(r["id"])

    # ------------------------------------------------------------------ delete / GC
    def release_file(self, file_id: int, conn) -> None:
        """Drop a file's chunk references (inside the caller's transaction)."""
        rows = conn.execute("SELECT chunk_hash FROM file_chunks WHERE file_id = ?", (file_id,)).fetchall()
        for r in rows:
            h = r["chunk_hash"]
            conn.execute("UPDATE chunks SET refcount = refcount - 1 WHERE hash = ?", (h,))
            row = conn.execute("SELECT refcount, size, batch_id FROM chunks WHERE hash = ?", (h,)).fetchone()
            if row is not None and row["refcount"] == 0:
                conn.execute("UPDATE batches SET live_bytes = live_bytes - ? WHERE id = ?", (row["size"], row["batch_id"]))
        conn.execute("DELETE FROM file_chunks WHERE file_id = ?", (file_id,))

    def compact(self, ratio: float | None = None) -> list[int]:
        """Rewrite sealed batches whose live ratio fell below the threshold."""
        ratio = self.config.batch_compact_ratio if ratio is None else ratio
        victims = [r["id"] for r in self.db.all(
            "SELECT id FROM batches WHERE state = 'sealed' AND raw_bytes > 0 AND "
            "CAST(live_bytes AS REAL) / raw_bytes < ?", (ratio,))]
        done = []
        for bid in victims:
            with self._lock:
                target = self._open_batch()
                raw = self._raw_path(target)
                live = self.db.all("SELECT hash, size FROM chunks WHERE batch_id = ? AND refcount > 0", (bid,))
                moved = []
                with open(raw, "ab") as out:
                    off = out.tell()
                    for r in live:
                        data = self.read_chunk(bytes(r["hash"]))
                        out.write(data)
                        moved.append((target, off, bytes(r["hash"])))
                        off += len(data)
                    out.flush()
                    if self.config.fsync:
                        os.fsync(out.fileno())
                total = sum(r["size"] for r in live)
                with self.db.tx() as c:
                    c.executemany("UPDATE chunks SET batch_id = ?, raw_offset = ? WHERE hash = ?", moved)
                    c.execute("DELETE FROM chunks WHERE batch_id = ?", (bid,))
                    c.execute("UPDATE batches SET raw_bytes = raw_bytes + ?, live_bytes = live_bytes + ?, "
                              "first_data_at = COALESCE(first_data_at, ?) WHERE id = ?",
                              (total, total, time.time(), target))
                    c.execute("UPDATE batches SET state = 'deleted', live_bytes = 0, object_id = NULL WHERE id = ?", (bid,))
                    c.execute("DELETE FROM batch_frames WHERE batch_id = ?", (bid,))
                self.store.delete(f"batch/{bid}")
                self._frames = OrderedDict((k, v) for k, v in self._frames.items() if k[0] != bid)
                self._dicts.pop(bid, None)
                done.append(bid)
                log.info("compacted batch %d: moved %d live bytes", bid, total)
        return done

    def stats(self) -> dict:
        r = self.db.one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM chunks WHERE refcount > 0")
        logical = self.db.scalar(
            "SELECT COALESCE(SUM(size),0) FROM files WHERE tier = 'nonmedia' AND purged_at IS NULL")
        b = self.db.one("SELECT COALESCE(SUM(raw_bytes),0) AS raw, COALESCE(SUM(compressed_bytes),0) AS comp "
                        "FROM batches WHERE state = 'sealed'")
        open_bytes = self.db.scalar("SELECT COALESCE(SUM(raw_bytes),0) FROM batches WHERE state IN ('open','sealing')")
        return {
            "logical_bytes": int(logical),
            "unique_chunk_bytes": int(r["bytes"]),
            "unique_chunks": int(r["n"]),
            "dedup_ratio": (logical / r["bytes"]) if r["bytes"] else None,
            "sealed_raw_bytes": int(b["raw"]),
            "sealed_compressed_bytes": int(b["comp"]),
            "compression_ratio": (b["raw"] / b["comp"]) if b["comp"] else None,
            "open_batch_bytes": int(open_bytes),
            "bloom": {"bits": self.bloom.m, "k": self.bloom.k, "items": self.bloom.count,
                      "expected_fpr": self.bloom.expected_fpr(), **self.bloom.stats},
        }
