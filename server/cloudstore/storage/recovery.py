"""Disaster recovery: rebuild the object/stripe/shard index from the disks (TDD §4.6).

Every shard header carries its stripe id, (k, m), stripe position and the object
key, so if the SQLite metadata file is lost the durable layer can be re-indexed
by scanning the disks. File-level metadata (EXIF, thumbnails, hashes) is then
re-derived from the originals by the ingest pipeline (``cloudstore recover-index
--reingest``).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from . import shardfile as sf
from .disks import READABLE, DiskManager
from ..db.database import Database

log = logging.getLogger(__name__)


def recover_index(db: Database, disks: DiskManager) -> dict:
    stripes: dict[str, dict] = {}
    shards: dict[str, dict[int, tuple[int, bytes]]] = defaultdict(dict)
    bad_headers = 0
    for d in disks.list(READABLE):
        for path in sf.iter_shard_files(d.path):
            try:
                h = sf.read_header(path)
            except (sf.CorruptShard, OSError):
                bad_headers += 1
                continue
            if not h.key:
                continue
            stripes.setdefault(h.stripe_id, {"key": h.key, "k": h.k, "m": h.m, "seq": h.seq,
                                             "offset": h.data_offset, "len": h.data_len,
                                             "shard_size": h.length})
            shards[h.stripe_id].setdefault(h.index, (d.id, h.checksum))

    known = {r["id"] for r in db.all("SELECT id FROM stripes")}
    by_key: dict[str, list[str]] = defaultdict(list)
    for sid, meta in stripes.items():
        if sid not in known:
            by_key[meta["key"]].append(sid)

    objects_added = stripes_added = 0
    now = time.time()
    for key, sids in by_key.items():
        sids.sort(key=lambda s: stripes[s]["seq"])
        with db.tx() as c:
            row = c.execute("SELECT id FROM objects WHERE key = ?", (key,)).fetchone()
            if row is None:
                size = max(stripes[s]["offset"] + stripes[s]["len"] for s in sids)
                oid = c.execute(
                    "INSERT INTO objects(key, size, stripe_count, state, created_at) VALUES(?,?,?, 'committed', ?)",
                    (key, size, len(sids), now)).lastrowid
                objects_added += 1
            else:
                oid = row["id"]
            for sid in sids:
                m = stripes[sid]
                c.execute(
                    "INSERT INTO stripes(id, object_id, seq, k, m, shard_size, data_offset, data_len, state, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?, 'committed', ?)",
                    (sid, oid, m["seq"], m["k"], m["m"], m["shard_size"], m["offset"], m["len"], now))
                for idx in range(m["k"] + m["m"]):
                    disk_id, csum = shards[sid].get(idx, (None, None))
                    c.execute(
                        "INSERT INTO shards(stripe_id, idx, disk_id, checksum, is_parity, state) VALUES(?,?,?,?,?,?)",
                        (sid, idx, disk_id, csum, int(idx >= m["k"]), "ok" if disk_id else "missing"))
                stripes_added += 1
    report = {"objects_added": objects_added, "stripes_added": stripes_added,
              "shards_seen": sum(len(v) for v in shards.values()), "bad_headers": bad_headers}
    log.info("index recovery: %s", report)
    return report
