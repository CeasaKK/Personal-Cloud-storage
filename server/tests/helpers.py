import base64
import hashlib
import io

import numpy as np
from PIL import Image


def make_jpeg(seed: int = 0, size=(640, 480), gps=None, when="2024:05:01 10:00:00", offset="+05:30",
              model="iPhone 15 Pro", variant: int = 0) -> bytes:
    """Procedural photo-like JPEG with EXIF. ``variant`` makes a near-duplicate
    (slight brightness/noise change) of the same scene."""
    rng = np.random.default_rng(seed)
    h, w = size[1], size[0]
    y, x = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w, 3), dtype=np.float64)
    for c in range(3):
        fx, fy, ph = rng.uniform(0.002, 0.02, 2).tolist() + [rng.uniform(0, 6.28)]
        img[..., c] = 127 + 100 * np.sin(x * fx + y * fy + ph)
    for _ in range(6):
        cx, cy, r = rng.integers(0, w), rng.integers(0, h), rng.integers(20, 120)
        mask = (x - cx) ** 2 + (y - cy) ** 2 < r * r
        img[mask] = rng.integers(0, 255, 3)
    if variant:
        vr = np.random.default_rng(1000 + variant)
        img = img * (1 + 0.03 * variant) + vr.normal(0, 3, img.shape)
    pil = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
    exif = Image.Exif()
    exif[0x010F] = "Apple"
    exif[0x0110] = model
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = when
    if offset:
        sub[0x9011] = offset
    if gps:
        lat, lon = gps
        g = exif.get_ifd(0x8825)
        g[1] = "N" if lat >= 0 else "S"
        g[2] = _dms(abs(lat))
        g[3] = "E" if lon >= 0 else "W"
        g[4] = _dms(abs(lon))
    buf = io.BytesIO()
    pil.save(buf, "JPEG", quality=90, exif=exif)
    return buf.getvalue()


def _dms(v: float):
    d = int(v)
    m = int((v - d) * 60)
    s = round(((v - d) * 60 - m) * 60, 4)
    return (float(d), float(m), float(s))


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def tus_meta(**kv) -> str:
    return ",".join(f"{k} {base64.b64encode(str(v).encode()).decode()}" for k, v in kv.items())


TUS = {"Tus-Resumable": "1.0.0"}


def tus_upload(client, data: bytes, filename: str, headers: dict, device_id: str | None = None,
               chunk: int | None = None, created_at: float | None = None):
    meta = {"filename": filename, "sha256": sha256(data)}
    if device_id:
        meta["device_id"] = device_id
    if created_at:
        meta["created_at"] = created_at
    r = client.post("/api/tus", headers={**headers, **TUS, "Upload-Length": str(len(data)),
                                         "Upload-Metadata": tus_meta(**meta)})
    assert r.status_code == 201, r.text
    loc = r.headers["location"]
    chunk = chunk or max(len(data), 1)
    off = 0
    last = None
    while off < len(data):
        part = data[off : off + chunk]
        last = client.patch(loc, content=part, headers={**headers, **TUS, "Upload-Offset": str(off),
                                                        "Content-Type": "application/offset+octet-stream"})
        assert last.status_code == 204, last.text
        off = int(last.headers["upload-offset"])
    return loc, last
