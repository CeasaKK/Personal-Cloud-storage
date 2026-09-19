"""Metadata snapshots (TDD §14.1).

SQLite lives on the mini PC's single system SSD. Shard headers alone can rebuild
the object layer (recovery.py), but albums, favourites, trash state and sync
manifests exist only in the database. So once a day the database is copied with
SQLite's online backup API, zstd-compressed, and stored as an erasure-coded object
``meta/snapshot-<unix time>`` on the data disks — the metadata gets the same
any-two-disks durability as the photos. The last ``keep`` snapshots are retained.

Disaster recovery after losing the SSD:
    cloudstore recover-index --restore-metadata
rebuilds the object index from the disks, finds the newest snapshot object among
the recovered keys, and installs it as the live database.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
import time
from pathlib import Path

import zstandard as zstd

from ..db.database import Database
from .objectstore import ObjectStore

log = logging.getLogger(__name__)
PREFIX = "meta/snapshot-"


def take_snapshot(db: Database, store: ObjectStore, keep: int = 7) -> str:
    with tempfile.TemporaryDirectory() as td:
        copy = Path(td) / "meta.sqlite3"
        dst = sqlite3.connect(copy)
        with dst:
            db.conn.backup(dst)  # consistent online copy, concurrent writers allowed
        dst.close()
        raw = copy.read_bytes()
    blob = zstd.ZstdCompressor(level=10).compress(raw)
    key = f"{PREFIX}{int(time.time())}"
    store.put_bytes(key, blob)
    old = [r["key"] for r in db.all("SELECT key FROM objects WHERE key LIKE ? AND state = 'committed' "
                                    "ORDER BY key DESC", (PREFIX + "%",))][keep:]
    for k in old:
        store.delete(k)
    log.info("metadata snapshot %s: %d bytes -> %d compressed", key, len(raw), len(blob))
    return key


def latest_snapshot(db: Database) -> str | None:
    return db.scalar("SELECT key FROM objects WHERE key LIKE ? AND state = 'committed' ORDER BY key DESC LIMIT 1",
                     (PREFIX + "%",))


def restore_snapshot(store: ObjectStore, key: str, target: Path) -> int:
    raw = zstd.ZstdDecompressor().decompress(store.get(key), max_output_size=1 << 34)
    tmp = target.with_suffix(".restore")
    tmp.write_bytes(raw)
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    tmp.replace(target)
    return len(raw)
