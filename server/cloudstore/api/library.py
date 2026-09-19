"""Albums, search, map clusters and near-duplicate review (TDD §9, §10)."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ..spatial import geohash
from .deps import file_summary, require_user, svc

router = APIRouter(prefix="/api", tags=["library"], dependencies=[Depends(require_user)])


# ------------------------------------------------------------------ albums
class AlbumBody(BaseModel):
    name: str
    cover_file_id: int | None = None


class AlbumItems(BaseModel):
    ids: list[int]


@router.get("/albums")
def albums(request: Request) -> dict:
    rows = svc(request).db.all(
        "SELECT a.*, (SELECT COUNT(*) FROM album_items ai JOIN files f ON f.id = ai.file_id "
        "             WHERE ai.album_id = a.id AND f.trashed_at IS NULL) AS count, "
        "COALESCE(a.cover_file_id, (SELECT ai.file_id FROM album_items ai JOIN files f ON f.id = ai.file_id "
        "  WHERE ai.album_id = a.id AND f.trashed_at IS NULL ORDER BY f.capture_ts DESC LIMIT 1)) AS cover "
        "FROM albums a ORDER BY a.updated_at DESC")
    return {"albums": [dict(r) for r in rows]}


@router.post("/albums")
def create_album(body: AlbumBody, request: Request) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "album name required")
    now = time.time()
    aid = svc(request).db.execute("INSERT INTO albums(name, cover_file_id, created_at, updated_at) VALUES(?,?,?,?)",
                                  (name, body.cover_file_id, now, now)).lastrowid
    return {"id": aid, "name": name}


@router.get("/albums/{album_id}")
def get_album(album_id: int, request: Request) -> dict:
    r = svc(request).db.one("SELECT * FROM albums WHERE id = ?", (album_id,))
    if r is None:
        raise HTTPException(404, "no such album")
    return dict(r)


@router.patch("/albums/{album_id}")
def rename_album(album_id: int, body: AlbumBody, request: Request) -> dict:
    s = svc(request)
    s.db.execute("UPDATE albums SET name = ?, cover_file_id = COALESCE(?, cover_file_id), updated_at = ? WHERE id = ?",
                 (body.name.strip(), body.cover_file_id, time.time(), album_id))
    return get_album(album_id, request)


@router.delete("/albums/{album_id}")
def delete_album(album_id: int, request: Request) -> dict:
    svc(request).db.execute("DELETE FROM albums WHERE id = ?", (album_id,))
    return {"ok": True}


@router.post("/albums/{album_id}/items")
def add_items(album_id: int, body: AlbumItems, request: Request) -> dict:
    s = svc(request)
    if s.db.one("SELECT 1 FROM albums WHERE id = ?", (album_id,)) is None:
        raise HTTPException(404, "no such album")
    now = time.time()
    with s.db.tx() as c:
        c.executemany("INSERT OR IGNORE INTO album_items(album_id, file_id, added_at) "
                      "SELECT ?, id, ? FROM files WHERE id = ? AND purged_at IS NULL",
                      [(album_id, now, i) for i in body.ids])
        c.execute("UPDATE albums SET updated_at = ? WHERE id = ?", (now, album_id))
    return {"ok": True}


@router.post("/albums/{album_id}/items/remove")
def remove_items(album_id: int, body: AlbumItems, request: Request) -> dict:
    s = svc(request)
    with s.db.tx() as c:
        c.executemany("DELETE FROM album_items WHERE album_id = ? AND file_id = ?", [(album_id, i) for i in body.ids])
    return {"ok": True}


# ------------------------------------------------------------------ search
@router.get("/search")
def search(request: Request,
           date_from: float | None = None, date_to: float | None = None,
           camera: str | None = None, q: str | None = None, kind: str | None = None,
           lat: float | None = None, lon: float | None = None, radius_km: float = Query(5.0, gt=0, le=2000),
           limit: int = Query(500, le=2000)) -> dict:
    s = svc(request)
    where = ["trashed_at IS NULL", "tier = 'media'"]
    params: list = []
    if date_from is not None:
        where.append("capture_ts >= ?")
        params.append(date_from)
    if date_to is not None:
        where.append("capture_ts < ?")
        params.append(date_to)
    if camera:
        where.append("(camera_model = ? OR camera_make = ?)")
        params += [camera, camera]
    if kind in ("image", "video"):
        where.append("kind = ?")
        params.append(kind)
    if q:
        where.append("filename LIKE ?")
        params.append(f"%{q}%")
    if lat is not None and lon is not None:
        cells = geohash.covering(lat, lon, radius_km)
        where.append("(" + " OR ".join("geohash LIKE ?" for _ in cells) + ")")
        params += [c + "%" for c in cells]
    rows = s.db.all(f"SELECT * FROM files WHERE {' AND '.join(where)} ORDER BY capture_ts DESC LIMIT ?",
                    (*params, limit * (4 if lat is not None else 1)))
    if lat is not None and lon is not None:
        rows = [r for r in rows if geohash.haversine_km(lat, lon, r["lat"], r["lon"]) <= radius_km][:limit]
    return {"items": [file_summary(r) for r in rows]}


@router.get("/search/facets")
def facets(request: Request) -> dict:
    s = svc(request)
    cams = s.db.all("SELECT camera_make AS make, camera_model AS model, COUNT(*) AS n FROM files "
                    "WHERE trashed_at IS NULL AND camera_model IS NOT NULL GROUP BY make, model ORDER BY n DESC")
    rng = s.db.one("SELECT MIN(capture_ts) AS lo, MAX(capture_ts) AS hi, COUNT(*) AS n FROM files "
                   "WHERE trashed_at IS NULL AND tier = 'media'")
    return {"cameras": [dict(c) for c in cams], "date_range": dict(rng)}


# ------------------------------------------------------------------ map
@router.get("/map/clusters")
def clusters(request: Request, west: float, south: float, east: float, north: float,
             zoom: int = Query(..., ge=0, le=22)) -> dict:
    out = svc(request).geo.clusters(west, south, east, north, zoom)
    return {"clusters": out, "total": sum(c["count"] for c in out)}


@router.get("/map/items")
def map_items(request: Request, west: float, south: float, east: float, north: float,
              limit: int = Query(200, le=1000)) -> dict:
    rows = svc(request).db.all(
        "SELECT * FROM files WHERE trashed_at IS NULL AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
        "ORDER BY capture_ts DESC LIMIT ?", (south, north, west, east, limit))
    return {"items": [file_summary(r) for r in rows]}


# ------------------------------------------------------------------ duplicates
class ResolveBody(BaseModel):
    keep: list[int]


@router.get("/duplicates")
def duplicates(request: Request) -> dict:
    groups = svc(request).dups.groups()
    return {"groups": groups, "reclaimable_bytes": sum(g["reclaimable_bytes"] for g in groups)}


@router.post("/duplicates/{group_id}/resolve")
def resolve(group_id: int, body: ResolveBody, request: Request) -> dict:
    s = svc(request)
    members = [r["id"] for r in s.db.all(
        "SELECT id FROM files WHERE dup_group = ? AND trashed_at IS NULL", (group_id,))]
    if not members:
        raise HTTPException(404, "no such group")
    keep = set(body.keep) & set(members)
    if not keep:
        raise HTTPException(400, "keep at least one member")
    trashed = s.files.trash([m for m in members if m not in keep])
    s.db.execute("UPDATE files SET dup_reviewed = 1 WHERE dup_group = ?", (group_id,))
    return {"trashed": trashed}


@router.post("/duplicates/{group_id}/dismiss")
def dismiss(group_id: int, request: Request) -> dict:
    svc(request).db.execute("UPDATE files SET dup_reviewed = 1 WHERE dup_group = ?", (group_id,))
    return {"ok": True}
