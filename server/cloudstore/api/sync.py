"""Device registration + Merkle sync protocol (TDD §7)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..sync.merkle import DEPTH
from .deps import require_user, svc

router = APIRouter(prefix="/api", tags=["sync"], dependencies=[Depends(require_user)])
PATH_RE = re.compile(r"^[0-9a-f]{0,%d}$" % DEPTH)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class DeviceBody(BaseModel):
    name: str
    platform: str = "ios"
    device_id: str | None = None


class PathsBody(BaseModel):
    paths: list[str] = Field(max_length=4096)


class ReconcileBody(BaseModel):
    add: list[str] = Field(default_factory=list, max_length=200_000)
    remove: list[str] = Field(default_factory=list, max_length=200_000)


def _device(request: Request, device_id: str):
    s = svc(request)
    if not s.sync.device_exists(device_id):
        raise HTTPException(404, "unknown device")
    return s.sync.tree(device_id)


@router.post("/devices")
def register(body: DeviceBody, request: Request) -> dict:
    did = svc(request).sync.register_device(body.name, body.platform, body.device_id)
    return {"device_id": did}


@router.get("/devices")
def devices(request: Request) -> dict:
    return {"devices": svc(request).sync.devices()}


@router.get("/sync/{device_id}/root")
def root(device_id: str, request: Request) -> dict:
    t = _device(request, device_id)
    return {"root": t.root().hex(), "count": len(t), "depth": DEPTH}


@router.post("/sync/{device_id}/nodes")
def nodes(device_id: str, body: PathsBody, request: Request) -> dict:
    t = _device(request, device_id)
    out = {}
    for p in body.paths:
        if not PATH_RE.match(p) or len(p) >= DEPTH:
            raise HTTPException(400, f"bad node path {p!r}")
        out[p] = [h.hex() for h in t.children(p)]
    return {"nodes": out}


@router.post("/sync/{device_id}/buckets")
def buckets(device_id: str, body: PathsBody, request: Request) -> dict:
    t = _device(request, device_id)
    out = {}
    for p in body.paths:
        if not PATH_RE.match(p) or len(p) != DEPTH:
            raise HTTPException(400, f"bad bucket path {p!r}")
        out[p] = t.bucket(p)
    return {"buckets": out}


@router.post("/sync/{device_id}/reconcile")
def reconcile(device_id: str, body: ReconcileBody, request: Request) -> dict:
    _device(request, device_id)
    for h in (*body.add, *body.remove):
        if not SHA_RE.match(h.lower()):
            raise HTTPException(400, f"bad hash {h!r}")
    res = svc(request).sync.reconcile(device_id, body.add, body.remove)
    res["root"] = svc(request).sync.tree(device_id).root().hex()
    return res
