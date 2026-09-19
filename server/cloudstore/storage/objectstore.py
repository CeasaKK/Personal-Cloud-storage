"""Erasure-coded object store (TDD §4.5–4.8).

An object (a media file or a sealed non-media batch) is a sequence of stripes.
Each stripe holds up to k * max_shard_size bytes, is Reed–Solomon encoded into
k data + m parity shards, and every shard of a stripe is placed on a distinct
disk. Stripes record their own (k, m), so mirror-profile and 4+2 stripes coexist.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

import numpy as np

from ..config import Config
from ..db.database import Database
from ..erasure.codec import NotEnoughShards, ReedSolomon
from . import shardfile as sf
from .disks import READABLE, WRITABLE, Disk, DiskManager, RETIRED

log = logging.getLogger(__name__)


class StoreError(Exception):
    pass


class NoDisks(StoreError):
    pass


class WriteFailed(StoreError):
    pass


class ObjectNotFound(StoreError):
    pass


class Unrecoverable(StoreError):
    pass


@dataclass
class StripeRow:
    id: str
    object_id: int
    seq: int
    k: int
    m: int
    shard_size: int
    data_offset: int
    data_len: int
    state: str

    @classmethod
    def from_row(cls, r) -> "StripeRow":
        return cls(r["id"], r["object_id"], r["seq"], r["k"], r["m"], r["shard_size"],
                   r["data_offset"], r["data_len"], r["state"])


@dataclass
class RepairResult:
    stripe_id: str
    verified: int = 0
    corrupt: int = 0
    missing: int = 0
    repaired: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    unrecoverable: bool = False


class _StripeCache:
    """Byte-budgeted LRU of decoded stripe data (serves sequential range reads)."""

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.used = 0
        self._d: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def put(self, key: str, value: bytes) -> None:
        if len(value) > self.budget:
            return
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self.used -= len(old)
            self._d[key] = value
            self.used += len(value)
            while self.used > self.budget:
                _, v = self._d.popitem(last=False)
                self.used -= len(v)

    def drop(self, key: str) -> None:
        with self._lock:
            v = self._d.pop(key, None)
            if v is not None:
                self.used -= len(v)


class ObjectStore:
    def __init__(self, db: Database, disks: DiskManager, config: Config) -> None:
        self.db = db
        self.disks = disks
        self.config = config
        self._codecs: dict[tuple[int, int], ReedSolomon] = {}
        self._cache = _StripeCache(config.stripe_cache_bytes)
        self._key_locks: dict[str, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()

    # ------------------------------------------------------------------ helpers
    def codec(self, k: int, m: int) -> ReedSolomon:
        rs = self._codecs.get((k, m))
        if rs is None:
            rs = self._codecs[(k, m)] = ReedSolomon(k, m)
        return rs

    def profile(self) -> tuple[int, int]:
        """(k, m) for new writes, from the registered (non-retired) disk topology.

        Health does not change the profile: with 6 disks and one failed, new
        stripes are still 4+2 and the shard destined for the failed disk is
        recorded missing and repaired later (TDD §4.7).
        """
        n = int(self.db.scalar("SELECT COUNT(*) FROM disks WHERE status != ?", (RETIRED,)))
        k, m = self.config.ec_k, self.config.ec_m
        if n >= k + m:
            return k, m
        if n >= 2:
            return 1, min(n - 1, 2)  # mirror profile
        if n == 1:
            return 1, 0
        raise NoDisks("no disks registered")

    def _lock_for(self, key: str) -> threading.Lock:
        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = self._key_locks[key] = threading.Lock()
            return lock

    def _disk_map(self) -> dict[int, Disk]:
        return {d.id: d for d in self.disks.list()}

    def _place(self, n: int, stripe_id: str, exclude: set[int] = frozenset()) -> list[Disk | None]:
        """Choose n distinct writable disks, most free space first, then rotate
        index assignment by stripe id so parity is spread across disks."""
        cands = [d for d in self.disks.writable() if d.id not in exclude]
        cands.sort(key=lambda d: (-(d.free_bytes or 0), d.id))
        chosen = sorted(cands[:n], key=lambda d: d.id)
        slots: list[Disk | None] = list(chosen) + [None] * (n - len(chosen))
        rot = int(stripe_id[:8], 16) % n
        return [slots[(i + rot) % n] for i in range(n)]

    # ------------------------------------------------------------------ write
    def exists(self, key: str) -> bool:
        return self.db.scalar("SELECT 1 FROM objects WHERE key = ? AND state = 'committed'", (key,)) is not None

    def object_id(self, key: str) -> int | None:
        return self.db.scalar("SELECT id FROM objects WHERE key = ? AND state = 'committed'", (key,))

    def put_bytes(self, key: str, data: bytes, profile: tuple[int, int] | None = None) -> int:
        view = memoryview(data)
        pos = 0

        def reader(n: int) -> bytes:
            nonlocal pos
            chunk = view[pos : pos + n]
            pos += len(chunk)
            return bytes(chunk)

        return self.put_stream(key, reader, profile)

    def put_file(self, key: str, path: str | Path, profile: tuple[int, int] | None = None) -> int:
        with open(path, "rb") as f:
            return self.put_stream(key, _exact_reader(f), profile)

    def put_stream(self, key: str, read: Callable[[int], bytes], profile: tuple[int, int] | None = None) -> int:
        """Store an object from a reader. Idempotent on key."""
        with self._lock_for(key):
            existing = self.object_id(key)
            if existing is not None:
                return existing
            self._purge_object_key(key)  # leftover pending attempt
            k, m = profile or self.profile()
            now = time.time()
            with self.db.tx() as c:
                obj_id = c.execute(
                    "INSERT INTO objects(key, size, stripe_count, state, created_at) VALUES(?,0,0,'pending',?)",
                    (key, now),
                ).lastrowid
            try:
                size, seq = 0, 0
                stripe_cap = k * self.config.max_shard_size
                while True:
                    buf = read(stripe_cap)
                    if not buf and seq > 0:
                        break
                    self._write_stripe(obj_id, seq, size, buf, k, m)
                    size += len(buf)
                    seq += 1
                    if len(buf) < stripe_cap:
                        break
                with self.db.tx() as c:
                    c.execute("UPDATE objects SET size = ?, stripe_count = ?, state = 'committed' WHERE id = ?",
                              (size, seq, obj_id))
                return obj_id
            except BaseException:
                self._delete_object_rows(obj_id)
                raise

    def _write_stripe(self, object_id: int, seq: int, offset: int, data: bytes, k: int, m: int,
                      state_after: str = "committed") -> str:
        rs = self.codec(k, m)
        shard_size = rs.shard_size_for(len(data), self.config.max_shard_size)
        shards = rs.encode_bytes(data, shard_size)
        stripe_id = uuid.uuid4().hex
        placement = self._place(k + m, stripe_id)
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO stripes(id, object_id, seq, k, m, shard_size, data_offset, data_len, state, created_at) "
                "VALUES(?,?,?,?,?,?,?,?, 'pending', ?)",
                (stripe_id, object_id, seq, k, m, shard_size, offset, len(data), time.time()),
            )
        rows = []
        landed = []
        for i, (shard, disk) in enumerate(zip(shards, placement)):
            csum = sf.checksum(shard)
            if disk is None:
                rows.append((stripe_id, i, None, csum, int(i >= k), "missing"))
                continue
            header = sf.ShardHeader(i, k, m, stripe_id, len(shard), csum)
            try:
                sf.write_shard(disk.path, header, shard, fsync=self.config.fsync)
                rows.append((stripe_id, i, disk.id, csum, int(i >= k), "ok"))
                landed.append((disk, i))
            except sf.DiskIOError as e:
                self.disks.record_error(disk.id, f"write failed: {e}")
                rows.append((stripe_id, i, None, csum, int(i >= k), "missing"))
        need = min(k + m, k + self.config.min_write_extra)
        if len(landed) < need:
            for disk, i in landed:
                sf.delete_shard(disk.path, stripe_id, i)
            self.db.execute("DELETE FROM stripes WHERE id = ?", (stripe_id,))
            raise WriteFailed(f"only {len(landed)}/{k + m} shards written, need {need}")
        with self.db.tx() as c:
            c.executemany(
                "INSERT INTO shards(stripe_id, idx, disk_id, checksum, is_parity, state) VALUES(?,?,?,?,?,?)", rows
            )
            c.execute("UPDATE stripes SET state = ? WHERE id = ?", (state_after, stripe_id))
        if len(landed) < k + m:
            log.warning("stripe %s committed degraded (%d/%d shards)", stripe_id, len(landed), k + m)
        return stripe_id

    # ------------------------------------------------------------------ read
    def _stripes(self, object_id: int) -> list[StripeRow]:
        rows = self.db.all(
            "SELECT * FROM stripes WHERE object_id = ? AND state IN ('committed','unrecoverable') ORDER BY seq",
            (object_id,),
        )
        return [StripeRow.from_row(r) for r in rows]

    def stat(self, key: str) -> dict:
        row = self.db.one("SELECT * FROM objects WHERE key = ? AND state = 'committed'", (key,))
        if row is None:
            raise ObjectNotFound(key)
        return dict(row)

    def get(self, key: str) -> bytes:
        info = self.stat(key)
        return b"".join(self.iter_range(key, 0, info["size"]))

    def read_range(self, key: str, offset: int, length: int) -> bytes:
        return b"".join(self.iter_range(key, offset, offset + length))

    def iter_range(self, key: str, start: int, end: int) -> Iterator[bytes]:
        """Yield bytes [start, end) of an object, touching only overlapping stripes."""
        info = self.stat(key)
        end = min(end, info["size"])
        if start >= end:
            return
        for st in self._stripes(info["id"]):
            s0, s1 = st.data_offset, st.data_offset + st.data_len
            if s1 <= start or s0 >= end:
                continue
            data = self.stripe_data(st)
            yield data[max(start, s0) - s0 : min(end, s1) - s0]

    def stripe_data(self, st: StripeRow) -> bytes:
        cached = self._cache.get(st.id)
        if cached is not None:
            return cached
        data = self._read_stripe(st)
        self._cache.put(st.id, data)
        return data

    def _shard_rows(self, stripe_id: str) -> list:
        return self.db.all("SELECT * FROM shards WHERE stripe_id = ? ORDER BY idx", (stripe_id,))

    def _mark_shard(self, stripe_id: str, idx: int, state: str) -> None:
        self.db.execute("UPDATE shards SET state = ? WHERE stripe_id = ? AND idx = ?", (state, stripe_id, idx))

    def _try_read(self, st: StripeRow, row, disk_map: dict[int, Disk]) -> np.ndarray | None:
        """Read one shard; on failure record why and return None."""
        disk = disk_map.get(row["disk_id"]) if row["disk_id"] is not None else None
        if disk is None or disk.status not in READABLE or row["state"] == "missing":
            return None
        try:
            _, payload = sf.read_shard(disk.path, st.id, row["idx"], row["checksum"])
            return np.frombuffer(payload, dtype=np.uint8)
        except sf.MissingShard:
            self._mark_shard(st.id, row["idx"], "missing")
        except sf.CorruptShard as e:
            log.warning("corrupt shard %s.%d on %s: %s", st.id, row["idx"], disk.label, e)
            self._mark_shard(st.id, row["idx"], "corrupt")
        except sf.DiskIOError as e:
            self.disks.record_error(disk.id, f"read failed: {e}")
        return None

    def _read_stripe(self, st: StripeRow) -> bytes:
        """Healthy path: k data shards, concatenate. Degraded: add parity, decode."""
        rs = self.codec(st.k, st.m)
        rows = self._shard_rows(st.id)
        disk_map = self._disk_map()
        got: dict[int, np.ndarray] = {}
        for row in rows[: st.k]:
            arr = self._try_read(st, row, disk_map)
            if arr is not None:
                got[row["idx"]] = arr
        if len(got) < st.k:
            for row in rows[st.k :]:
                arr = self._try_read(st, row, disk_map)
                if arr is not None:
                    got[row["idx"]] = arr
                if len(got) >= st.k:
                    break
        try:
            return rs.decode_bytes(got, st.data_len)
        except NotEnoughShards as e:
            self.db.execute("UPDATE stripes SET state = 'unrecoverable' WHERE id = ?", (st.id,))
            raise Unrecoverable(f"stripe {st.id}: {e}") from e

    # ------------------------------------------------------------------ delete
    def delete(self, key: str) -> bool:
        with self._lock_for(key):
            row = self.db.one("SELECT id FROM objects WHERE key = ?", (key,))
            if row is None:
                return False
            self._delete_object_rows(row["id"])
            return True

    def _purge_object_key(self, key: str) -> None:
        row = self.db.one("SELECT id FROM objects WHERE key = ? AND state != 'committed'", (key,))
        if row is not None:
            self._delete_object_rows(row["id"])

    def _delete_stripe_files(self, stripe_ids: list[str]) -> None:
        disk_map = self._disk_map()
        for sid in stripe_ids:
            self._cache.drop(sid)
            for r in self._shard_rows(sid):
                d = disk_map.get(r["disk_id"]) if r["disk_id"] is not None else None
                if d is not None:
                    sf.delete_shard(d.path, sid, r["idx"])

    def _delete_object_rows(self, object_id: int) -> None:
        stripe_ids = [r["id"] for r in self.db.all("SELECT id FROM stripes WHERE object_id = ?", (object_id,))]
        # Rows first (so a crash leaves orphan files, never dangling rows), then files.
        shard_rows = {sid: self._shard_rows(sid) for sid in stripe_ids}
        with self.db.tx() as c:
            c.execute("DELETE FROM objects WHERE id = ?", (object_id,))
        disk_map = self._disk_map()
        for sid, rows in shard_rows.items():
            self._cache.drop(sid)
            for r in rows:
                d = disk_map.get(r["disk_id"]) if r["disk_id"] is not None else None
                if d is not None:
                    sf.delete_shard(d.path, sid, r["idx"])

    # ------------------------------------------------------------------ repair
    def repair_stripe(self, stripe_id: str, force_move_from: set[int] = frozenset()) -> RepairResult:
        """Verify every shard of a stripe and rebuild the bad ones (TDD §4.9–4.10).

        Shards on disks in ``force_move_from`` (a disk being replaced) are
        rewritten elsewhere even if readable. A rebuilt shard goes back to its own
        disk when that disk is writable, otherwise to a writable disk that holds
        no shard of this stripe.
        """
        row = self.db.one("SELECT * FROM stripes WHERE id = ?", (stripe_id,))
        res = RepairResult(stripe_id)
        if row is None or row["state"] not in ("committed", "unrecoverable"):
            return res
        st = StripeRow.from_row(row)
        rs = self.codec(st.k, st.m)
        rows = self._shard_rows(st.id)
        disk_map = self._disk_map()
        good: dict[int, np.ndarray] = {}
        to_write = []
        for r in rows:
            arr = self._try_read(st, r, disk_map)
            if arr is None:
                state = self.db.scalar("SELECT state FROM shards WHERE stripe_id = ? AND idx = ?", (st.id, r["idx"]))
                if state == "corrupt":
                    res.corrupt += 1
                else:
                    res.missing += 1
                to_write.append(r)
                continue
            good[r["idx"]] = arr
            res.verified += 1
            res.bytes_read += len(arr)
            if r["disk_id"] in force_move_from:
                to_write.append(r)
        now = time.time()
        if not to_write:
            self.db.execute("UPDATE stripes SET last_scrubbed_at = ?, state = 'committed' WHERE id = ?", (now, st.id))
            return res
        lost = [r["idx"] for r in to_write if r["idx"] not in good]
        try:
            payloads = rs.reconstruct(good, lost) if lost else {}
        except NotEnoughShards:
            self.db.execute("UPDATE stripes SET state = 'unrecoverable', last_scrubbed_at = ? WHERE id = ?",
                            (now, st.id))
            res.unrecoverable = True
            log.error("stripe %s is unrecoverable (%d/%d shards good)", st.id, len(good), st.k)
            return res
        for r in to_write:
            if r["idx"] in good:
                payloads[r["idx"]] = good[r["idx"]]
        rewrite = {r["idx"] for r in to_write}
        used = {r["disk_id"] for r in rows if r["disk_id"] is not None and r["idx"] not in rewrite}
        for r in to_write:
            idx = r["idx"]
            payload = payloads[idx]
            csum = sf.checksum(payload)
            target = disk_map.get(r["disk_id"]) if r["disk_id"] is not None else None
            if target is None or target.status not in WRITABLE or target.id in force_move_from or target.id in used:
                target = next((d for d in self._place(1, st.id, exclude=used | set(force_move_from)) if d), None)
            if target is None:
                log.warning("no writable disk available to repair %s.%d", st.id, idx)
                continue
            header = sf.ShardHeader(idx, st.k, st.m, st.id, len(payload), csum)
            try:
                sf.write_shard(target.path, header, payload, fsync=self.config.fsync)
            except sf.DiskIOError as e:
                self.disks.record_error(target.id, f"repair write failed: {e}")
                continue
            old = disk_map.get(r["disk_id"]) if r["disk_id"] is not None else None
            if old is not None and old.id != target.id and old.status in READABLE:
                sf.delete_shard(old.path, st.id, idx)
            self.db.execute(
                "UPDATE shards SET disk_id = ?, checksum = ?, state = 'ok' WHERE stripe_id = ? AND idx = ?",
                (target.id, csum, st.id, idx),
            )
            used.add(target.id)
            res.repaired += 1
            res.bytes_written += len(payload)
        self.db.execute("UPDATE stripes SET last_scrubbed_at = ?, state = 'committed' WHERE id = ?", (now, st.id))
        self._cache.drop(st.id)
        return res

    def stripes_needing_repair(self, limit: int = 100) -> list[str]:
        rows = self.db.all(
            "SELECT DISTINCT s.stripe_id FROM shards s JOIN stripes t ON t.id = s.stripe_id "
            "WHERE s.state != 'ok' AND t.state = 'committed' LIMIT ?",
            (limit,),
        )
        return [r[0] for r in rows]

    # ------------------------------------------------------------------ restripe
    def restripe_object(self, object_id: int, k: int, m: int) -> bool:
        """Re-encode an object's stripes under a new (k, m) profile (TDD §3 growth)."""
        old = self._stripes(object_id)
        if all((s.k, s.m) == (k, m) for s in old):
            return False
        new_ids = []
        try:
            for s in old:
                data = self._read_stripe(s)
                new_ids.append(self._write_stripe(object_id, s.seq, s.data_offset, data, k, m, state_after="pending"))
        except BaseException:
            self._delete_stripe_files(new_ids)
            if new_ids:
                self.db.execute("DELETE FROM stripes WHERE id IN (%s)" % ",".join("?" * len(new_ids)), new_ids)
            raise
        old_ids = [s.id for s in old]
        old_rows = {sid: self._shard_rows(sid) for sid in old_ids}
        with self.db.tx() as c:
            c.execute("UPDATE stripes SET state = 'committed' WHERE id IN (%s)" % ",".join("?" * len(new_ids)), new_ids)
            c.execute("DELETE FROM stripes WHERE id IN (%s)" % ",".join("?" * len(old_ids)), old_ids)
        disk_map = self._disk_map()
        for sid, rows in old_rows.items():
            self._cache.drop(sid)
            for r in rows:
                d = disk_map.get(r["disk_id"]) if r["disk_id"] is not None else None
                if d is not None:
                    sf.delete_shard(d.path, sid, r["idx"])
        return True

    def restripe_all(self, limit: int | None = None) -> int:
        k, m = self.profile()
        rows = self.db.all(
            "SELECT DISTINCT object_id FROM stripes WHERE state = 'committed' AND (k != ? OR m != ?)", (k, m)
        )
        done = 0
        for r in rows[:limit]:
            if self.restripe_object(r["object_id"], k, m):
                done += 1
        return done

    # ------------------------------------------------------------------ recovery
    def recover_pending(self) -> int:
        """Startup crash recovery: drop never-committed stripes and objects."""
        pending = self.db.all("SELECT id, k, m FROM stripes WHERE state = 'pending'")
        stripe_ids = [r["id"] for r in pending]
        # pending stripes have no shard rows yet (rows are inserted at commit): sweep files by name
        disks = self.disks.list(READABLE)
        for r in pending:
            for d in disks:
                for i in range(r["k"] + r["m"]):
                    sf.delete_shard(d.path, r["id"], i)
        if stripe_ids:
            self.db.execute("DELETE FROM stripes WHERE state = 'pending'")
        objs = [r["id"] for r in self.db.all("SELECT id FROM objects WHERE state = 'pending'")]
        for oid in objs:
            self._delete_object_rows(oid)
        for d in disks:
            for tmp in (d.path / "tmp").glob("*.part"):
                tmp.unlink(missing_ok=True)
        return len(stripe_ids) + len(objs)

    # ------------------------------------------------------------------ stats
    def capacity(self) -> dict:
        disks = [d for d in self.disks.list() if d.status != RETIRED]
        raw = sum(d.capacity_bytes or 0 for d in disks)
        free = sum(d.free_bytes or 0 for d in disks if d.status in READABLE)
        k, m = self.profile() if disks else (self.config.ec_k, self.config.ec_m)
        stored = int(self.db.scalar("SELECT COALESCE(SUM(size),0) FROM objects WHERE state = 'committed'"))
        on_disk = int(self.db.scalar(
            "SELECT COALESCE(SUM(t.shard_size + 64),0) FROM shards s JOIN stripes t ON t.id = s.stripe_id "
            "WHERE s.state = 'ok'"))
        return {
            "profile": {"k": k, "m": m, "mode": "mirror" if k == 1 else "erasure"},
            "raw_bytes": raw,
            "usable_bytes": int(raw * k / (k + m)) if raw else 0,
            "free_raw_bytes": free,
            "free_usable_bytes": int(free * k / (k + m)) if free else 0,
            "logical_stored_bytes": stored,
            "physical_stored_bytes": on_disk,
            "disks_tolerated": m,
        }

    def stripe_health(self) -> dict:
        rows = self.db.all("SELECT state, COUNT(*) AS n FROM stripes GROUP BY state")
        out = {r["state"]: r["n"] for r in rows}
        out["with_bad_shards"] = int(self.db.scalar(
            "SELECT COUNT(DISTINCT stripe_id) FROM shards WHERE state != 'ok'"))
        return out


def _exact_reader(f: BinaryIO) -> Callable[[int], bytes]:
    def read(n: int) -> bytes:
        parts = []
        while n > 0:
            b = f.read(n)
            if not b:
                break
            parts.append(b)
            n -= len(b)
        return b"".join(parts)
    return read
