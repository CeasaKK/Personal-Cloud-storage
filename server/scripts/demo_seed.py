"""Seed a local demo instance: 6 virtual disks, a password, and a generated library.

    cd server
    CLOUDSTORE_DATA_DIR=./demo/data python scripts/demo_seed.py
    CLOUDSTORE_DATA_DIR=./demo/data cloudstore serve     # then open http://127.0.0.1:8000

Password: demo-password-123. Photos are procedurally generated with realistic EXIF
(camera, capture time with offset, GPS across several cities) including burst
sequences, so the timeline, map, search and duplicate views all have content.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudstore.auth import Auth  # noqa: E402
from cloudstore.config import Config  # noqa: E402
from cloudstore.services import Services  # noqa: E402

PASSWORD = "demo-password-123"
PLACES = [
    ("New Delhi", 28.6139, 77.2090), ("Mumbai", 19.0760, 72.8777), ("Goa", 15.4909, 73.8278),
    ("Bengaluru", 12.9716, 77.5946), ("Jaipur", 26.9124, 75.7873), ("Paris", 48.8566, 2.3522),
    ("Tokyo", 35.6762, 139.6503), ("Rishikesh", 30.0869, 78.2676),
]
CAMERAS = [("Apple", "iPhone 15 Pro"), ("Apple", "iPhone 13"), ("Sony", "ILCE-7M3")]


def scene(rng: np.random.Generator, w: int, h: int, palette: tuple[float, float, float]) -> np.ndarray:
    y, x = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w, 3))
    horizon = rng.uniform(0.35, 0.65) * h
    for c in range(3):
        sky = 150 + 90 * palette[c] - 60 * (y / h)
        ground = 60 + 110 * palette[2 - c] * (0.6 + 0.4 * np.sin(x * rng.uniform(0.004, 0.02) + c))
        img[..., c] = np.where(y < horizon + 25 * np.sin(x * 0.01 + rng.uniform(0, 6)), sky, ground)
    for _ in range(rng.integers(3, 8)):
        cx, cy, r = rng.integers(0, w), rng.integers(int(h * 0.3), h), rng.integers(20, 140)
        img[(x - cx) ** 2 + (y - cy) ** 2 < r * r] = rng.integers(20, 240, 3)
    return img


def jpeg(arr: np.ndarray, *, when: str, offset: str, gps, camera, quality=90) -> bytes:
    pil = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    exif = Image.Exif()
    exif[0x010F], exif[0x0110] = camera
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = when
    sub[0x9011] = offset
    sub[0x829D] = 1.8  # FNumber
    sub[0x8827] = random.choice([50, 100, 200, 400])  # ISO
    if gps:
        lat, lon = gps
        g = exif.get_ifd(0x8825)
        g[1], g[3] = ("N" if lat >= 0 else "S"), ("E" if lon >= 0 else "W")
        for tag, v in ((2, abs(lat)), (4, abs(lon))):
            d = int(v)
            m = int((v - d) * 60)
            g[tag] = (float(d), float(m), round(((v - d) * 60 - m) * 60, 3))
    b = io.BytesIO()
    pil.save(b, "JPEG", quality=quality, exif=exif)
    return b.getvalue()


def main() -> None:
    cfg = Config()
    cfg.enable_workers = False
    svc = Services(cfg)
    disks_root = cfg.data_dir.parent / "disks"
    if not svc.disks.list():
        for i in range(1, 7):
            svc.disks.add(disks_root / f"d{i}", f"disk-{i}")
    auth = Auth(svc.db, cfg)
    if not auth.password_set():
        auth.set_password(PASSWORD)
    device = svc.sync.register_device("Demo iPhone", "ios", device_id="demo-iphone")

    rng = np.random.default_rng(42)
    random.seed(42)
    staged = 0

    def stage(data: bytes, filename: str, created: float | None = None) -> None:
        nonlocal staged
        sha = hashlib.sha256(data).hexdigest()
        if svc.db.one("SELECT 1 FROM files WHERE sha256 = ?", (sha,)):
            return
        p = cfg.staging_dir / "uploads" / f"demo-{sha[:16]}.part"
        p.write_bytes(data)
        meta = {"filename": filename, "device_id": device}
        if created:
            meta["created_at"] = created
        svc.ingest.enqueue(p, sha, meta)
        staged += 1

    n = 0
    for day in range(0, 720, 9):
        place = PLACES[(day // 45) % len(PLACES)]
        cam = CAMERAS[(day // 120) % len(CAMERAS)]
        month, dom = 1 + (day // 30) % 12, 1 + day % 27
        year = 2024 + day // 360
        palette = tuple(rng.uniform(0, 1, 3))
        shots = rng.integers(1, 4)
        for s in range(shots):
            base = scene(rng, 1440, 1080 if rng.random() < 0.8 else 1920, palette)
            hh, mm = 8 + int(rng.integers(0, 12)), int(rng.integers(0, 60))
            gps = (place[1] + rng.normal(0, 0.03), place[2] + rng.normal(0, 0.03)) if rng.random() < 0.85 else None
            when = f"{year}:{month:02d}:{dom:02d} {hh:02d}:{mm:02d}:00"
            burst = rng.random() < 0.18
            for b in range(3 if burst else 1):
                arr = base * (1 + 0.025 * b) + rng.normal(0, 3, base.shape) if b else base + rng.normal(0, 3, base.shape)
                stage(jpeg(arr, when=f"{when[:-2]}{b:02d}", offset="+05:30", gps=gps, camera=cam),
                      f"IMG_{4000 + n:04d}.JPG")
                n += 1
    # a few documents for the Files view
    docs = {
        "trip-plan.md": "# Goa trip\n\n- Flights booked\n- Stay: Anjuna\n" * 200,
        "budget-2025.csv": "month,rent,food,travel\n" + "\n".join(f"{m},25000,12000,{3000 + 97 * m}" for m in range(1, 13)) * 50,
        "notes.txt": ("Remember to scrub the disks monthly. " * 40 + "\n") * 60,
    }
    for name, text in docs.items():
        stage(text.encode(), name)
    stage(json.dumps({"exported": True, "items": list(range(5000))}).encode(), "export.json")

    print(f"staged {staged} files; ingesting (erasure-coding across 6 virtual disks)…")
    while svc.ingest.run_pending(limit=500):
        pass
    for r in svc.db.all("SELECT id FROM batches WHERE state = 'open' AND raw_bytes > 0"):
        svc.batches.seal(r["id"])
    run = svc.scrubber.run(full=True)
    print(json.dumps({"capacity": svc.store.capacity(), "scrub": run}, indent=2))
    print(f"\nDone. Start the server with CLOUDSTORE_DATA_DIR={cfg.data_dir} cloudstore serve")
    print(f"and sign in with password: {PASSWORD}")


if __name__ == "__main__":
    main()
