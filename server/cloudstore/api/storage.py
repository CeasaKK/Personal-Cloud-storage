"""Storage dashboard (TDD §10): capacity, disks, scrub, rebuild, tiers, ingest."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..erasure.backends import get_backend
from .deps import require_user, svc

router = APIRouter(prefix="/api/storage", tags=["storage"], dependencies=[Depends(require_user)])


@router.get("/overview")
def overview(request: Request) -> dict:
    s = svc(request)
    disks = []
    for r in s.db.all("SELECT * FROM disks ORDER BY id"):
        d = dict(r)
        d["smart"] = json.loads(d.pop("smart_json")) if d.get("smart_json") else None
        d["shards"] = s.db.scalar("SELECT COUNT(*) FROM shards WHERE disk_id = ?", (r["id"],))
        disks.append(d)
    media = s.db.one("SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM files "
                     "WHERE tier='media' AND purged_at IS NULL")
    nonmedia = s.db.one("SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM files "
                        "WHERE tier='nonmedia' AND purged_at IS NULL")
    scrubs = [dict(r) for r in s.db.all("SELECT * FROM scrub_runs ORDER BY id DESC LIMIT 20")]
    last_full = s.db.one("SELECT MIN(COALESCE(last_scrubbed_at, 0)) AS oldest FROM stripes WHERE state = 'committed'")
    rebuilds = [s.rebuild.status(r["id"]) for r in s.db.all("SELECT id FROM rebuild_tasks ORDER BY id DESC LIMIT 10")]
    ingest = {r["state"]: r["n"] for r in s.db.all("SELECT state, COUNT(*) AS n FROM ingest_jobs GROUP BY state")}
    return {
        "capacity": s.store.capacity(),
        "stripes": s.store.stripe_health(),
        "disks": disks,
        "tiers": {"media": dict(media), "nonmedia": dict(nonmedia), "nonmedia_store": s.batches.stats()},
        "scrub": {"runs": scrubs, "oldest_verified_at": last_full["oldest"] if last_full else None,
                  "rate_bytes_s": s.config.scrub_rate_bytes, "interval_days": s.config.scrub_interval_days},
        "rebuilds": rebuilds,
        "ingest": ingest,
        "thumb_cache": s.thumbs.cache.stats(),
        "gf_backend": get_backend().name,
        "devices": s.sync.devices(),
    }


@router.post("/scrub")
def scrub_now(request: Request) -> dict:
    s = svc(request)
    if s.workers is None:
        run = s.scrubber.run(full=True)
        return {"started": True, "run": run}
    s.workers.scrub_now.set()
    return {"started": True}


@router.post("/health-check")
def health_check(request: Request) -> dict:
    return {"disks": svc(request).disks.check_all(with_smart=True)}


class ReplaceBody(BaseModel):
    new_path: str
    label: str | None = None


@router.post("/disks/{disk_id}/replace")
def replace(disk_id: int, body: ReplaceBody, request: Request) -> dict:
    s = svc(request)
    try:
        task = s.rebuild.replace_disk(disk_id, body.new_path, body.label)
    except KeyError as e:
        raise HTTPException(404, "no such disk") from e
    return {"task_id": task}


@router.get("/ingest")
def ingest_jobs(request: Request, limit: int = 50) -> dict:
    rows = svc(request).db.all("SELECT id, sha256, state, attempts, error, file_id, created_at, updated_at, metadata "
                               "FROM ingest_jobs ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = dict(r)
        d["filename"] = json.loads(d.pop("metadata")).get("filename")
        out.append(d)
    return {"jobs": out}
