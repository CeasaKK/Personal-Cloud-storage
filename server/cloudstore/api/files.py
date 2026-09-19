"""Timeline, file detail, media bytes (with HTTP Range), favourites, trash (TDD §10)."""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .deps import file_detail, file_summary, require_user, svc

router = APIRouter(prefix="/api", tags=["files"], dependencies=[Depends(require_user)])
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


class IdsBody(BaseModel):
    ids: list[int]


class FilePatch(BaseModel):
    favorite: bool | None = None


def _cursor(cur: str | None) -> tuple[float, int] | None:
    if not cur:
        return None
    try:
        ts, fid = cur.split("_")
        return float(ts), int(fid)
    except ValueError as e:
        raise HTTPException(400, "bad cursor") from e


@router.get("/timeline")
def timeline(request: Request, cursor: str | None = None, limit: int = Query(120, le=500),
             kind: str | None = None, favorite: bool = False, album: int | None = None) -> dict:
    """Keyset pagination on (capture_ts DESC, id DESC): stable under inserts, O(limit)."""
    s = svc(request)
    where = ["f.trashed_at IS NULL", "f.tier = 'media'"]
    params: list = []
    join = ""
    if kind in ("image", "video"):
        where.append("f.kind = ?")
        params.append(kind)
    if favorite:
        where.append("f.favorite = 1")
    if album is not None:
        join = "JOIN album_items ai ON ai.file_id = f.id AND ai.album_id = ?"
        params.insert(0, album)
    c = _cursor(cursor)
    if c:
        where.append("(f.capture_ts < ? OR (f.capture_ts = ? AND f.id < ?))")
        params += [c[0], c[0], c[1]]
    rows = s.db.all(f"SELECT f.* FROM files f {join} WHERE {' AND '.join(where)} "
                    f"ORDER BY f.capture_ts DESC, f.id DESC LIMIT ?", (*params, limit + 1))
    items = [file_summary(r) for r in rows[:limit]]
    nxt = f"{rows[limit - 1]['capture_ts']}_{rows[limit - 1]['id']}" if len(rows) > limit else None
    return {"items": items, "next_cursor": nxt}


@router.get("/timeline/months")
def months(request: Request) -> dict:
    rows = svc(request).db.all(
        "SELECT strftime('%Y-%m', capture_ts, 'unixepoch') AS month, COUNT(*) AS n, MAX(capture_ts) AS latest "
        "FROM files WHERE trashed_at IS NULL AND tier = 'media' GROUP BY month ORDER BY month DESC")
    return {"months": [dict(r) for r in rows]}


@router.get("/files")
def list_files(request: Request, tier: str = "nonmedia", cursor: str | None = None,
               limit: int = Query(200, le=1000)) -> dict:
    s = svc(request)
    params: list = [tier]
    cond = ""
    c = _cursor(cursor)
    if c:
        cond = "AND (upload_ts < ? OR (upload_ts = ? AND id < ?))"
        params += [c[0], c[0], c[1]]
    rows = s.db.all(f"SELECT * FROM files WHERE tier = ? AND trashed_at IS NULL {cond} "
                    f"ORDER BY upload_ts DESC, id DESC LIMIT ?", (*params, limit + 1))
    nxt = f"{rows[limit - 1]['upload_ts']}_{rows[limit - 1]['id']}" if len(rows) > limit else None
    return {"items": [file_summary(r) for r in rows[:limit]], "next_cursor": nxt}


@router.get("/files/by-hash/{sha256}")
def by_hash(sha256: str, request: Request) -> dict:
    r = svc(request).db.one("SELECT * FROM files WHERE sha256 = ?", (sha256.lower(),))
    if r is None:
        job = svc(request).db.one("SELECT state, error FROM ingest_jobs WHERE sha256 = ? ORDER BY id DESC LIMIT 1",
                                  (sha256.lower(),))
        if job is not None:
            return {"status": job["state"], "error": job["error"]}
        raise HTTPException(404, "unknown hash")
    return {"status": "stored", "file": file_detail(r)}


@router.get("/files/{file_id}")
def get_file(file_id: int, request: Request) -> dict:
    s = svc(request)
    r = s.files.get(file_id)
    if r is None:
        raise HTTPException(404, "no such file")
    d = file_detail(r)
    d["albums"] = [dict(a) for a in s.db.all(
        "SELECT a.id, a.name FROM albums a JOIN album_items ai ON ai.album_id = a.id WHERE ai.file_id = ?", (file_id,))]
    return d


@router.patch("/files/{file_id}")
def patch_file(file_id: int, body: FilePatch, request: Request) -> dict:
    s = svc(request)
    if s.files.get(file_id) is None:
        raise HTTPException(404, "no such file")
    if body.favorite is not None:
        s.db.execute("UPDATE files SET favorite = ? WHERE id = ?", (int(body.favorite), file_id))
    return file_detail(s.files.get(file_id))


@router.get("/files/{file_id}/thumb")
def thumb(file_id: int, request: Request) -> Response:
    return _derived(file_id, "thumb", request)


@router.get("/files/{file_id}/proxy")
def proxy(file_id: int, request: Request) -> Response:
    return _derived(file_id, "proxy", request)


def _derived(file_id: int, variant: str, request: Request) -> Response:
    try:
        data, mime = svc(request).thumbs.get(file_id, variant)
    except KeyError as e:
        raise HTTPException(404, "no such file") from e
    # content-addressed underneath, so the browser may cache aggressively
    return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=31536000, immutable"})


@router.get("/files/{file_id}/original")
def original(file_id: int, request: Request, download: bool = False):
    s = svc(request)
    f = s.files.get(file_id)
    if f is None or f["purged_at"] is not None:
        raise HTTPException(404, "no such file")
    size = f["size"]
    start, end = 0, size
    status = 200
    rng = request.headers.get("range")
    if rng:
        m = RANGE_RE.match(rng.strip())
        if not m or (not m.group(1) and not m.group(2)):
            raise HTTPException(416, "bad range", headers={"Content-Range": f"bytes */{size}"})
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)) + 1, size) if m.group(2) else size
        else:  # suffix range: last N bytes
            start = max(size - int(m.group(2)), 0)
        if start >= size or start >= end:
            raise HTTPException(416, "range not satisfiable", headers={"Content-Range": f"bytes */{size}"})
        status = 206
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start),
               "Cache-Control": "private, max-age=31536000, immutable", "ETag": f'"{f["sha256"]}"'}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end - 1}/{size}"
    disp = "attachment" if download else "inline"
    headers["Content-Disposition"] = f"{disp}; filename*=UTF-8''{quote(f['filename'])}"

    # An Unrecoverable raised mid-stream aborts the connection, so the client sees a
    # truncated body rather than silently wrong bytes.
    return StreamingResponse(s.files.iter_bytes(f, start, end), status_code=status, media_type=f["mime"],
                             headers=headers)


@router.post("/files/trash")
def trash(body: IdsBody, request: Request) -> dict:
    return {"trashed": svc(request).files.trash(body.ids)}


@router.post("/files/restore")
def restore(body: IdsBody, request: Request) -> dict:
    return {"restored": svc(request).files.restore(body.ids)}


@router.get("/trash")
def list_trash(request: Request) -> dict:
    s = svc(request)
    rows = s.db.all("SELECT * FROM files WHERE trashed_at IS NOT NULL AND purged_at IS NULL ORDER BY trashed_at DESC")
    return {"items": [file_summary(r) for r in rows], "retention_days": s.config.trash_retention_days}


@router.post("/trash/purge")
def purge(request: Request, body: IdsBody | None = None) -> dict:
    s = svc(request)
    if body and body.ids:
        ids = [i for i in body.ids if (r := s.files.get(i)) is not None and r["trashed_at"] is not None]
    else:
        ids = [r["id"] for r in s.db.all("SELECT id FROM files WHERE trashed_at IS NOT NULL AND purged_at IS NULL")]
    return {"purged": s.files.purge(ids)}
