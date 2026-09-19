"""Near-duplicate grouping (TDD §9).

In-memory BK-tree over the pHash of every live image, rebuilt at startup.
A new image queries radius r; every hit's group is union-merged with the new
image's group (union-find by smallest file id as root), persisted in
``files.dup_group``.
"""

from __future__ import annotations

import threading

from ..db.database import Database
from .bktree import BKTree
from .hashing import to_unsigned


class DuplicateIndex:
    def __init__(self, db: Database, radius: int = 10) -> None:
        self.db = db
        self.radius = radius
        self.tree = BKTree()
        self._lock = threading.Lock()

    def load(self) -> int:
        with self._lock:
            self.tree = BKTree()
            rows = self.db.all("SELECT id, phash FROM files WHERE phash IS NOT NULL AND trashed_at IS NULL")
            for r in rows:
                self.tree.add(to_unsigned(r["phash"]), r["id"])
            return len(rows)

    def add(self, file_id: int, phash: int) -> int | None:
        """Index a new image; return its duplicate group id (None if unique)."""
        with self._lock:
            hits = [i for i, _ in self.tree.search(phash, self.radius) if i != file_id]
            self.tree.add(phash, file_id)
        if not hits:
            return None
        ids = [file_id] + hits
        groups = {r["dup_group"] for r in self.db.all(
            "SELECT dup_group FROM files WHERE id IN (%s) AND dup_group IS NOT NULL" % ",".join("?" * len(hits)), hits)}
        root = min([file_id, *hits, *groups])
        with self.db.tx() as c:
            if groups:
                c.execute("UPDATE files SET dup_group = ? WHERE dup_group IN (%s)" % ",".join("?" * len(groups)),
                          (root, *groups))
            c.execute("UPDATE files SET dup_group = ?, dup_reviewed = 0 WHERE id IN (%s)" % ",".join("?" * len(ids)),
                      (root, *ids))
        return root

    def remove(self, file_id: int, phash: int | None) -> None:
        if phash is None:
            return
        with self._lock:
            self.tree.remove(to_unsigned(phash), file_id)

    def groups(self, include_reviewed: bool = False) -> list[dict]:
        cond = "" if include_reviewed else "AND dup_reviewed = 0"
        rows = self.db.all(
            f"SELECT id, dup_group, size, width, height, sharpness, capture_ts, filename, mime, kind "
            f"FROM files WHERE dup_group IS NOT NULL AND trashed_at IS NULL {cond} ORDER BY dup_group, capture_ts")
        by_group: dict[int, list] = {}
        for r in rows:
            by_group.setdefault(r["dup_group"], []).append(dict(r))
        out = []
        for gid, members in by_group.items():
            if len(members) < 2:
                continue
            keeper = max(members, key=lambda m: ((m["width"] or 0) * (m["height"] or 0), m["sharpness"] or 0,
                                                 -m["capture_ts"]))
            reclaim = sum(m["size"] for m in members if m["id"] != keeper["id"])
            out.append({"group_id": gid, "members": members, "suggested_keep": keeper["id"],
                        "reclaimable_bytes": reclaim})
        out.sort(key=lambda g: -g["reclaimable_bytes"])
        return out
