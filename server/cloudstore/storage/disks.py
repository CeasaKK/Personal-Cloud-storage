"""Disk registry and health monitoring (TDD §3, §4.10).

A disk is a mounted filesystem root identified by the ``disk.json`` identity
file written when it is registered — never by its mount path, which can change
between boots.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..db.database import Database

log = logging.getLogger(__name__)

ONLINE, DEGRADED, FAILED, REBUILDING, RETIRED = "online", "degraded", "failed", "rebuilding", "retired"
WRITABLE = (ONLINE, REBUILDING)
READABLE = (ONLINE, DEGRADED, REBUILDING)


@dataclass
class Disk:
    id: int
    uuid: str
    label: str
    path: Path
    status: str
    error_count: int
    capacity_bytes: int | None
    free_bytes: int | None

    @classmethod
    def from_row(cls, r) -> "Disk":
        return cls(r["id"], r["uuid"], r["label"], Path(r["path"]), r["status"], r["error_count"],
                   r["capacity_bytes"], r["free_bytes"])


class DiskManager:
    def __init__(self, db: Database, error_threshold: int = 10) -> None:
        self.db = db
        self.error_threshold = error_threshold

    # -- registry -----------------------------------------------------------
    def add(self, path: str | Path, label: str | None = None) -> Disk:
        root = Path(path).resolve()
        root.mkdir(parents=True, exist_ok=True)
        ident = root / "disk.json"
        if ident.exists():
            meta = json.loads(ident.read_text())
            existing = self.db.one("SELECT * FROM disks WHERE uuid = ?", (meta["uuid"],))
            if existing is not None:
                if Path(existing["path"]) != root:
                    self.db.execute("UPDATE disks SET path = ? WHERE id = ?", (str(root), existing["id"]))
                return self.get(existing["id"])
        else:
            meta = {"uuid": uuid.uuid4().hex, "label": label or root.name, "created_at": time.time()}
            ident.write_text(json.dumps(meta))
        (root / "shards").mkdir(exist_ok=True)
        (root / "tmp").mkdir(exist_ok=True)
        usage = shutil.disk_usage(root)
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO disks(uuid, label, path, status, capacity_bytes, free_bytes, added_at, last_seen_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (meta["uuid"], label or meta.get("label") or root.name, str(root), ONLINE,
                 usage.total, usage.free, time.time(), time.time()),
            )
            disk_id = cur.lastrowid
        log.info("registered disk %s at %s", meta["uuid"], root)
        return self.get(disk_id)

    def get(self, disk_id: int) -> Disk:
        row = self.db.one("SELECT * FROM disks WHERE id = ?", (disk_id,))
        if row is None:
            raise KeyError(disk_id)
        return Disk.from_row(row)

    def list(self, statuses: tuple[str, ...] | None = None) -> list[Disk]:
        if statuses:
            q = "SELECT * FROM disks WHERE status IN (%s) ORDER BY id" % ",".join("?" * len(statuses))
            rows = self.db.all(q, statuses)
        else:
            rows = self.db.all("SELECT * FROM disks ORDER BY id")
        return [Disk.from_row(r) for r in rows]

    def writable(self) -> list[Disk]:
        return self.list(WRITABLE)

    def readable_ids(self) -> set[int]:
        return {d.id for d in self.list(READABLE)}

    def set_status(self, disk_id: int, status: str, error: str | None = None) -> None:
        self.db.execute("UPDATE disks SET status = ?, last_error = COALESCE(?, last_error) WHERE id = ?",
                        (status, error, disk_id))

    def record_error(self, disk_id: int, message: str) -> None:
        """Count an I/O error; demote the disk once past the threshold."""
        self.db.execute("UPDATE disks SET error_count = error_count + 1, last_error = ? WHERE id = ?",
                        (message[:500], disk_id))
        d = self.get(disk_id)
        if d.status == ONLINE and d.error_count >= self.error_threshold:
            log.warning("disk %s exceeded error threshold, marking degraded", d.label)
            self.set_status(disk_id, DEGRADED)

    # -- health -------------------------------------------------------------
    def probe(self, disk: Disk) -> tuple[bool, str | None]:
        """Identity + write/read probe. Returns (healthy, error)."""
        ident = disk.path / "disk.json"
        try:
            meta = json.loads(ident.read_text())
            if meta.get("uuid") != disk.uuid:
                return False, "identity mismatch (wrong disk mounted?)"
            probe = disk.path / "tmp" / ".probe"
            token = os.urandom(16)
            probe.write_bytes(token)
            if probe.read_bytes() != token:
                return False, "probe read-back mismatch"
            probe.unlink()
            return True, None
        except FileNotFoundError:
            return False, "disk root or identity file missing (unmounted?)"
        except (OSError, ValueError) as e:
            return False, f"probe failed: {e}"

    def smart(self, disk: Disk) -> dict | None:
        """Best-effort SMART summary via smartctl (needs root + SAT passthrough on USB)."""
        smartctl = shutil.which("smartctl")
        if not smartctl:
            return None
        try:
            dev = subprocess.run(["findmnt", "-no", "SOURCE", "--target", str(disk.path)],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            if not dev:
                return None
            out = subprocess.run([smartctl, "-j", "-H", "-A", dev], capture_output=True, text=True, timeout=20)
            data = json.loads(out.stdout or "{}")
            return {
                "passed": data.get("smart_status", {}).get("passed"),
                "temperature": data.get("temperature", {}).get("current"),
                "power_on_hours": data.get("power_on_time", {}).get("hours"),
                "reallocated": next((a["raw"]["value"] for a in data.get("ata_smart_attributes", {}).get("table", [])
                                     if a.get("id") == 5), None),
            }
        except Exception:  # pragma: no cover - hardware dependent
            return None

    def check_all(self, with_smart: bool = False) -> list[dict]:
        """Run one health pass; transition disk states. Returns a report."""
        report = []
        for d in self.list():
            if d.status == RETIRED:
                continue
            ok, err = self.probe(d)
            now = time.time()
            if ok:
                usage = shutil.disk_usage(d.path)
                smart = self.smart(d) if with_smart else None
                self.db.execute(
                    "UPDATE disks SET last_seen_at = ?, capacity_bytes = ?, free_bytes = ?, "
                    "smart_json = COALESCE(?, smart_json) WHERE id = ?",
                    (now, usage.total, usage.free, json.dumps(smart) if smart else None, d.id),
                )
                if d.status == FAILED:
                    # Came back (e.g. enclosure reconnected). Shards written meanwhile
                    # were placed elsewhere; scrub reconciles anything stale.
                    log.warning("disk %s is reachable again, marking online", d.label)
                    self.set_status(d.id, ONLINE)
                if smart and smart.get("passed") is False and d.status == ONLINE:
                    self.set_status(d.id, DEGRADED, "SMART health check failed")
            elif d.status != FAILED:
                log.error("disk %s failed health check: %s", d.label, err)
                self.set_status(d.id, FAILED, err)
            report.append({"id": d.id, "label": d.label, "ok": ok, "error": err})
        return report
