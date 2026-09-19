"""tus 1.0.0 resumable upload server (TDD §8).

Extensions: creation, termination, expiration, checksum-on-completion via the
``sha256`` metadata key (a mismatch returns 460, per the tus checksum
extension). Required metadata: ``filename`` and ``sha256``; optional
``mimetype``, ``device_id``, ``created_at`` (unix seconds), ``local_id``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import threading
import time
import uuid
from email.utils import formatdate

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..ingest.pipeline import sha256_file
from .deps import require_user

TUS_VERSION = "1.0.0"
EXTENSIONS = "creation,termination,expiration"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
FLUSH_EVERY = 8 * 1024 * 1024

router = APIRouter(prefix="/api/tus", tags=["upload"])
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(upload_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(upload_id, threading.Lock())


def _headers(extra: dict | None = None) -> dict:
    h = {"Tus-Resumable": TUS_VERSION, "Cache-Control": "no-store"}
    if extra:
        h.update(extra)
    return h


def parse_metadata(raw: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not raw:
        return out
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(" ", 1)
        key = parts[0]
        try:
            out[key] = base64.b64decode(parts[1]).decode() if len(parts) > 1 else ""
        except (ValueError, UnicodeDecodeError) as e:
            raise HTTPException(400, f"bad Upload-Metadata for {key}", headers=_headers()) from e
    return out


def _check_version(request: Request) -> None:
    if request.headers.get("tus-resumable") != TUS_VERSION:
        raise HTTPException(412, "unsupported tus version", headers=_headers({"Tus-Version": TUS_VERSION}))


def _staging(request: Request, upload_id: str):
    return request.app.state.svc.config.staging_dir / "uploads" / f"{upload_id}.part"


@router.options("")
@router.options("/")
def options(request: Request) -> Response:
    cfg = request.app.state.svc.config
    return Response(status_code=204, headers=_headers({
        "Tus-Version": TUS_VERSION, "Tus-Extension": EXTENSIONS, "Tus-Max-Size": str(cfg.max_upload_size)}))


@router.post("", dependencies=[Depends(require_user)])
@router.post("/", dependencies=[Depends(require_user)], include_in_schema=False)
def create(request: Request) -> Response:
    _check_version(request)
    svc = request.app.state.svc
    try:
        length = int(request.headers.get("upload-length", ""))
    except ValueError as e:
        raise HTTPException(400, "Upload-Length required", headers=_headers()) from e
    if length < 0 or length > svc.config.max_upload_size:
        raise HTTPException(413, "upload too large", headers=_headers())
    meta = parse_metadata(request.headers.get("upload-metadata"))
    sha = meta.get("sha256", "").lower()
    if not SHA_RE.match(sha) or not meta.get("filename"):
        raise HTTPException(400, "metadata must include filename and sha256", headers=_headers())
    meta["sha256"] = sha
    device_id = meta.get("device_id") or None
    if device_id and not svc.sync.device_exists(device_id):
        raise HTTPException(400, "unknown device_id; register the device first", headers=_headers())
    upload_id = uuid.uuid4().hex
    now = time.time()
    expires = now + svc.config.upload_expiry_s
    _staging(request, upload_id).touch()
    svc.db.execute("INSERT INTO uploads(id, length, offset, metadata, device_id, created_at, expires_at) "
                   "VALUES(?,?,0,?,?,?,?)", (upload_id, length, json.dumps(meta), device_id, now, expires))
    # relative Location: correct behind `tailscale serve` or any reverse proxy
    location = f"/api/tus/{upload_id}"
    headers = _headers({"Location": location, "Upload-Expires": formatdate(expires, usegmt=True)})
    if length == 0:
        _complete(request, upload_id)
        headers["Upload-Offset"] = "0"
    return Response(status_code=201, headers=headers)


def _row(request: Request, upload_id: str):
    row = request.app.state.svc.db.one("SELECT * FROM uploads WHERE id = ?", (upload_id,))
    if row is None or (row["completed_at"] is None and row["expires_at"] < time.time()):
        raise HTTPException(404, "unknown or expired upload", headers=_headers())
    return row


@router.head("/{upload_id}", name="tus_head", dependencies=[Depends(require_user)])
def head(upload_id: str, request: Request) -> Response:
    row = _row(request, upload_id)
    return Response(status_code=200, headers=_headers({
        "Upload-Offset": str(row["offset"]), "Upload-Length": str(row["length"]),
        "Upload-Expires": formatdate(row["expires_at"], usegmt=True)}))


@router.patch("/{upload_id}", dependencies=[Depends(require_user)])
async def patch(upload_id: str, request: Request) -> Response:
    _check_version(request)
    if request.headers.get("content-type") != "application/offset+octet-stream":
        raise HTTPException(415, "Content-Type must be application/offset+octet-stream", headers=_headers())
    lock = _lock(upload_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(423, "upload is locked by another request", headers=_headers())
    try:
        row = _row(request, upload_id)
        if row["completed_at"] is not None:
            return Response(status_code=204, headers=_headers({"Upload-Offset": str(row["offset"])}))
        try:
            client_offset = int(request.headers.get("upload-offset", ""))
        except ValueError as e:
            raise HTTPException(400, "Upload-Offset required", headers=_headers()) from e
        if client_offset != row["offset"]:
            raise HTTPException(409, "offset mismatch", headers=_headers({"Upload-Offset": str(row["offset"])}))
        db = request.app.state.svc.db
        path = _staging(request, upload_id)
        offset = row["offset"]
        length = row["length"]
        f = open(path, "r+b")
        try:
            f.seek(offset)
            f.truncate()
            since_flush = 0
            async for part in request.stream():
                if offset + len(part) > length:
                    raise HTTPException(413, "body exceeds Upload-Length", headers=_headers())
                await asyncio.to_thread(f.write, part)
                offset += len(part)
                since_flush += len(part)
                if since_flush >= FLUSH_EVERY:
                    f.flush()
                    os.fsync(f.fileno())  # never record an offset the disk doesn't have
                    db.execute("UPDATE uploads SET offset = ? WHERE id = ?", (offset, upload_id))
                    since_flush = 0
        finally:
            # persist whatever arrived, so an interrupted PATCH resumes from here
            f.flush()
            os.fsync(f.fileno())
            f.close()
            db.execute("UPDATE uploads SET offset = ? WHERE id = ?", (offset, upload_id))
        if offset == length:
            await asyncio.to_thread(_complete, request, upload_id)
        return Response(status_code=204, headers=_headers({"Upload-Offset": str(offset)}))
    finally:
        lock.release()


def _complete(request: Request, upload_id: str) -> None:
    svc = request.app.state.svc
    row = svc.db.one("SELECT * FROM uploads WHERE id = ?", (upload_id,))
    meta = json.loads(row["metadata"])
    path = _staging(request, upload_id)
    actual = sha256_file(path)
    if actual != meta["sha256"]:
        path.unlink(missing_ok=True)
        svc.db.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
        raise HTTPException(460, f"checksum mismatch: expected {meta['sha256']}, got {actual}", headers=_headers())
    svc.ingest.enqueue(path, actual, meta)
    svc.db.execute("UPDATE uploads SET completed_at = ? WHERE id = ?", (time.time(), upload_id))


@router.delete("/{upload_id}", dependencies=[Depends(require_user)])
def terminate(upload_id: str, request: Request) -> Response:
    _check_version(request)
    row = _row(request, upload_id)
    if row["completed_at"] is None:
        _staging(request, upload_id).unlink(missing_ok=True)
    request.app.state.svc.db.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
    return Response(status_code=204, headers=_headers())
