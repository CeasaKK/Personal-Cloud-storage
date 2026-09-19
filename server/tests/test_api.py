import os

import pytest
from fastapi.testclient import TestClient

from cloudstore.app import create_app
from cloudstore.auth import Auth
from cloudstore.services import Services
from cloudstore.sync.merkle import MerkleTree, diff

from .conftest import make_config
from .helpers import TUS, make_jpeg, sha256, tus_meta, tus_upload

PASSWORD = "correct horse battery staple"


@pytest.fixture
def app_env(tmp_path):
    cfg = make_config(tmp_path, max_shard_size=64 * 1024, batch_threshold=10**12)
    svc = Services(cfg)
    for i in range(6):
        svc.disks.add(tmp_path / f"disk{i}", f"d{i}")
    Auth(svc.db, cfg).set_password(PASSWORD)
    app = create_app(cfg, svc)
    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={"password": PASSWORD, "client": "ios"})
        assert r.status_code == 200
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        yield client, svc, headers, r.json()


def ingest(svc):
    svc.ingest.run_pending()


def test_auth_required(app_env):
    client, *_ = app_env
    assert client.get("/api/timeline").status_code == 401
    assert client.get("/api/health").status_code == 200


def test_login_rejects_bad_password(app_env):
    client, *_ = app_env
    assert client.post("/api/auth/login", json={"password": "nope", "client": "ios"}).status_code == 401


def test_refresh_rotation_and_reuse_detection(app_env):
    client, svc, headers, tokens = app_env
    r1 = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r1.status_code == 200
    # reusing the rotated token revokes the family, including the new token
    assert client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    assert client.post("/api/auth/refresh", json={"refresh_token": r1.json()["refresh_token"]}).status_code == 401


def test_web_cookie_auth_requires_csrf_header(app_env):
    client, *_ = app_env
    r = client.post("/api/auth/login", json={"password": PASSWORD, "client": "web"})
    assert r.status_code == 200 and "cs_access" in r.cookies
    assert client.get("/api/timeline").status_code == 200
    assert client.post("/api/albums", json={"name": "x"}).status_code == 403
    assert client.post("/api/albums", json={"name": "x"}, headers={"X-Requested-With": "fetch"}).status_code == 200
    client.cookies.clear()


def test_photo_upload_ingest_and_browse(app_env):
    client, svc, h, _ = app_env
    did = client.post("/api/devices", json={"name": "iPhone", "platform": "ios"}, headers=h).json()["device_id"]
    photo = make_jpeg(1, gps=(28.6139, 77.2090))
    tus_upload(client, photo, "IMG_0001.JPG", h, device_id=did, chunk=50_000)
    ingest(svc)
    tl = client.get("/api/timeline", headers=h).json()
    assert len(tl["items"]) == 1
    f = tl["items"][0]
    assert f["kind"] == "image" and f["tier"] == "media" and f["has_location"]
    detail = client.get(f"/api/files/{f['id']}", headers=h).json()
    assert detail["camera_model"] == "iPhone 15 Pro"
    assert abs(detail["lat"] - 28.6139) < 1e-3 and abs(detail["lon"] - 77.2090) < 1e-3
    # 2024-05-01 10:00:00 +05:30 == 04:30 UTC
    assert detail["capture_ts"] == 1714537800.0
    assert client.get(f"/api/files/{f['id']}/thumb", headers=h).headers["content-type"] == "image/webp"
    assert client.get(f"/api/files/{f['id']}/proxy", headers=h).status_code == 200
    orig = client.get(f"/api/files/{f['id']}/original", headers=h)
    assert orig.content == photo
    part = client.get(f"/api/files/{f['id']}/original", headers={**h, "Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == photo[100:200]
    tail = client.get(f"/api/files/{f['id']}/original", headers={**h, "Range": "bytes=-50"})
    assert tail.content == photo[-50:]
    # stored erasure-coded across 6 disks
    assert svc.db.scalar("SELECT COUNT(DISTINCT disk_id) FROM shards") == 6
    # map + search
    cl = client.get("/api/map/clusters", params=dict(west=-180, south=-85, east=180, north=85, zoom=3), headers=h).json()
    assert cl["total"] == 1
    near = client.get("/api/search", params=dict(lat=28.62, lon=77.21, radius_km=5), headers=h).json()
    assert len(near["items"]) == 1
    far = client.get("/api/search", params=dict(lat=19.07, lon=72.87, radius_km=5), headers=h).json()
    assert far["items"] == []
    assert client.get("/api/search", params=dict(camera="iPhone 15 Pro"), headers=h).json()["items"]


def test_tus_resume_and_checksum(app_env):
    client, svc, h, _ = app_env
    data = make_jpeg(2)
    meta = tus_meta(filename="a.jpg", sha256=sha256(data))
    loc = client.post("/api/tus", headers={**h, **TUS, "Upload-Length": str(len(data)), "Upload-Metadata": meta}
                      ).headers["location"]
    patch_h = {**h, **TUS, "Content-Type": "application/offset+octet-stream"}
    client.patch(loc, content=data[:1000], headers={**patch_h, "Upload-Offset": "0"})
    # connection "dropped"; client asks where to resume
    head = client.head(loc, headers={**h, **TUS})
    assert head.headers["upload-offset"] == "1000"
    assert client.patch(loc, content=data[5:10], headers={**patch_h, "Upload-Offset": "5"}).status_code == 409
    r = client.patch(loc, content=data[1000:], headers={**patch_h, "Upload-Offset": "1000"})
    assert r.status_code == 204
    ingest(svc)
    assert client.get(f"/api/files/by-hash/{sha256(data)}", headers=h).json()["status"] == "stored"
    # wrong checksum
    bad = client.post("/api/tus", headers={**h, **TUS, "Upload-Length": "3",
                                           "Upload-Metadata": tus_meta(filename="b", sha256="0" * 64)})
    r = client.patch(bad.headers["location"], content=b"abc", headers={**patch_h, "Upload-Offset": "0"})
    assert r.status_code == 460
    opts = client.options("/api/tus")
    assert "creation" in opts.headers["tus-extension"]


def test_merkle_sync_protocol(app_env):
    client, svc, h, _ = app_env
    did = client.post("/api/devices", json={"name": "phone"}, headers=h).json()["device_id"]
    photos = [make_jpeg(10 + i) for i in range(5)]
    for i, p in enumerate(photos[:3]):
        tus_upload(client, p, f"p{i}.jpg", h, device_id=did)
    ingest(svc)
    local = MerkleTree(sha256(p) for p in photos)  # phone has 5, server has 3

    def children(paths):
        return {k: [bytes.fromhex(x) for x in v] for k, v in
                client.post(f"/api/sync/{did}/nodes", json={"paths": paths}, headers=h).json()["nodes"].items()}

    def buckets(paths):
        return client.post(f"/api/sync/{did}/buckets", json={"paths": paths}, headers=h).json()["buckets"]

    root = bytes.fromhex(client.get(f"/api/sync/{did}/root", headers=h).json()["root"])
    missing, extra = diff(local, root, children, buckets)
    assert missing == {sha256(p) for p in photos[3:]} and extra == set()
    rec = client.post(f"/api/sync/{did}/reconcile", json={"add": sorted(missing), "remove": []}, headers=h).json()
    assert sorted(rec["need_upload"]) == sorted(missing)
    for i, p in enumerate(photos[3:]):
        tus_upload(client, p, f"q{i}.jpg", h, device_id=did)
    ingest(svc)
    assert client.get(f"/api/sync/{did}/root", headers=h).json()["root"] == local.root().hex()

    # a second device with the same photo: linked without upload (cross-device dedup)
    d2 = client.post("/api/devices", json={"name": "ipad"}, headers=h).json()["device_id"]
    rec = client.post(f"/api/sync/{d2}/reconcile", json={"add": [sha256(photos[0])]}, headers=h).json()
    assert rec["linked"] == 1 and rec["need_upload"] == []


def test_deleted_on_server_is_never_reuploaded(app_env):
    client, svc, h, _ = app_env
    did = client.post("/api/devices", json={"name": "phone"}, headers=h).json()["device_id"]
    p = make_jpeg(40)
    tus_upload(client, p, "x.jpg", h, device_id=did)
    ingest(svc)
    fid = client.get("/api/timeline", headers=h).json()["items"][0]["id"]
    client.post("/api/files/trash", json={"ids": [fid]}, headers=h)
    assert client.get("/api/timeline", headers=h).json()["items"] == []
    assert len(client.get("/api/trash", headers=h).json()["items"]) == 1
    client.post("/api/files/restore", json={"ids": [fid]}, headers=h)
    assert len(client.get("/api/timeline", headers=h).json()["items"]) == 1
    client.post("/api/files/trash", json={"ids": [fid]}, headers=h)
    assert client.post("/api/trash/purge", json={"ids": [fid]}, headers=h).json()["purged"] == 1
    assert svc.db.scalar("SELECT COUNT(*) FROM shards") == 0  # bytes gone
    rec = client.post(f"/api/sync/{did}/reconcile", json={"add": [sha256(p)]}, headers=h).json()
    assert rec["need_upload"] == []  # tombstone remembers the deletion


def test_near_duplicates_grouped(app_env):
    client, svc, h, _ = app_env
    burst = [make_jpeg(7, variant=v) for v in range(3)]
    other = make_jpeg(99)
    for i, b in enumerate(burst + [other]):
        tus_upload(client, b, f"burst{i}.jpg", h)
    ingest(svc)
    groups = client.get("/api/duplicates", headers=h).json()["groups"]
    assert len(groups) == 1 and len(groups[0]["members"]) == 3
    g = groups[0]
    r = client.post(f"/api/duplicates/{g['group_id']}/resolve", json={"keep": [g["suggested_keep"]]}, headers=h)
    assert r.json()["trashed"] == 2
    assert client.get("/api/duplicates", headers=h).json()["groups"] == []


def test_nonmedia_tier_dedup_seal_and_read(app_env):
    client, svc, h, _ = app_env
    # high-entropy content with a fixed seed. (Periodic data such as b"ab" * N has no
    # content-defined boundaries: CDC falls back to max-size cuts, which do shift on insert.)
    import numpy as np
    doc = np.random.default_rng(5).bytes(800_000)
    doc2 = doc[:150_000] + b"EDIT" + doc[150_000:]
    tus_upload(client, doc, "notes.txt", h)
    tus_upload(client, doc2, "notes-v2.txt", h)
    ingest(svc)
    files = client.get("/api/files", params={"tier": "nonmedia"}, headers=h).json()["items"]
    assert len(files) == 2 and all(f["tier"] == "nonmedia" for f in files)
    stats = svc.batches.stats()
    assert stats["unique_chunk_bytes"] < len(doc) + len(doc2) * 0.5  # v2 mostly deduplicated
    by_name = {f["filename"]: f["id"] for f in files}
    assert client.get(f"/api/files/{by_name['notes.txt']}/original", headers=h).content == doc
    bid = svc.db.scalar("SELECT id FROM batches WHERE state = 'open'")
    rep = svc.batches.seal(bid)
    assert rep["compressed_bytes"] < rep["raw_bytes"]
    svc.batches._frames.clear()
    assert client.get(f"/api/files/{by_name['notes-v2.txt']}/original", headers=h).content == doc2
    rng = client.get(f"/api/files/{by_name['notes-v2.txt']}/original", headers={**h, "Range": "bytes=149990-150010"})
    assert rng.content == doc2[149990:150011]
    # purge one; compaction keeps the other readable
    client.post("/api/files/trash", json={"ids": [by_name["notes.txt"]]}, headers=h)
    client.post("/api/trash/purge", headers=h)
    svc.batches.compact(ratio=1.01)
    svc.batches._frames.clear()
    assert client.get(f"/api/files/{by_name['notes-v2.txt']}/original", headers=h).content == doc2


def test_albums_favorites_and_dashboard(app_env):
    client, svc, h, _ = app_env
    for i in range(3):
        tus_upload(client, make_jpeg(50 + i), f"a{i}.jpg", h)
    ingest(svc)
    ids = [f["id"] for f in client.get("/api/timeline", headers=h).json()["items"]]
    aid = client.post("/api/albums", json={"name": "Goa"}, headers=h).json()["id"]
    client.post(f"/api/albums/{aid}/items", json={"ids": ids[:2]}, headers=h)
    assert len(client.get("/api/timeline", params={"album": aid}, headers=h).json()["items"]) == 2
    assert client.get("/api/albums", headers=h).json()["albums"][0]["count"] == 2
    client.patch(f"/api/files/{ids[0]}", json={"favorite": True}, headers=h)
    assert [f["id"] for f in client.get("/api/timeline", params={"favorite": True}, headers=h).json()["items"]] == [ids[0]]
    ov = client.get("/api/storage/overview", headers=h).json()
    assert ov["capacity"]["profile"]["k"] == 4 and len(ov["disks"]) == 6
    assert ov["tiers"]["media"]["n"] == 3
    run = client.post("/api/storage/scrub", headers=h).json()["run"]
    assert run["status"] == "done" and run["corrupt_found"] == 0


def test_timeline_pagination(app_env):
    client, svc, h, _ = app_env
    for i in range(7):
        tus_upload(client, make_jpeg(200 + i, size=(64, 48)), f"t{i}.jpg", h, created_at=1_700_000_000 + i)
    ingest(svc)
    seen, cursor = [], None
    while True:
        r = client.get("/api/timeline", params={"limit": 3, **({"cursor": cursor} if cursor else {})}, headers=h).json()
        seen += [f["id"] for f in r["items"]]
        cursor = r["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 7
