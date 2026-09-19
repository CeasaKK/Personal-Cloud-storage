"""Shared API dependencies: service lookup and authentication (TDD §11)."""

from __future__ import annotations

import json

from fastapi import HTTPException, Request

from ..auth import AuthError
from ..services import Services

ACCESS_COOKIE = "cs_access"
REFRESH_COOKIE = "cs_refresh"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def svc(request: Request) -> Services:
    return request.app.state.svc


def require_user(request: Request) -> dict:
    auth = request.app.state.auth
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    else:
        token = request.cookies.get(ACCESS_COOKIE)
        if token and request.method not in SAFE_METHODS and not request.headers.get("x-requested-with"):
            # cookie auth on a state-changing request must prove it came from our JS (CSRF)
            raise HTTPException(403, "missing X-Requested-With header")
    if not token:
        raise HTTPException(401, "not authenticated", headers={"WWW-Authenticate": "Bearer"})
    try:
        return auth.verify_access(token)
    except AuthError as e:
        raise HTTPException(401, str(e), headers={"WWW-Authenticate": "Bearer"}) from e


def file_summary(r) -> dict:
    return {
        "id": r["id"], "sha256": r["sha256"], "kind": r["kind"], "mime": r["mime"], "tier": r["tier"],
        "filename": r["filename"], "size": r["size"], "capture_ts": r["capture_ts"],
        "width": r["width"], "height": r["height"], "duration": r["duration"],
        "favorite": bool(r["favorite"]), "dup_group": r["dup_group"],
        "has_location": r["lat"] is not None, "trashed_at": r["trashed_at"],
    }


def file_detail(r) -> dict:
    d = file_summary(r)
    d.update({
        "upload_ts": r["upload_ts"], "camera_make": r["camera_make"], "camera_model": r["camera_model"],
        "lat": r["lat"], "lon": r["lon"], "exif": json.loads(r["exif_json"]) if r["exif_json"] else {},
        "purged_at": r["purged_at"],
    })
    return d
