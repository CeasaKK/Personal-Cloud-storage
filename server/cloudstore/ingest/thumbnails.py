"""Thumbnail (256 px WebP) and proxy (1600 px JPEG) generation (TDD §5, §10).

Derived files live on the SSD under ``derived/<variant>/<sha[:2]>/<sha>.<ext>``;
they are regenerable from the originals and never erasure-coded.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

log = logging.getLogger(__name__)

VARIANTS = {"thumb": ("webp", "image/webp"), "proxy": ("jpg", "image/jpeg")}


def derived_path(root: Path, variant: str, sha256: str) -> Path:
    ext = VARIANTS[variant][0]
    return root / variant / sha256[:2] / f"{sha256}.{ext}"


def open_oriented(path: Path) -> Image.Image:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    elif img.mode == "L":
        img = img.convert("RGB")
    return img


def write_derivatives(img: Image.Image, root: Path, sha256: str, thumb_size: int, proxy_size: int) -> dict[str, Path]:
    out = {}
    thumb = img.copy()
    thumb.thumbnail((thumb_size, thumb_size), Image.Resampling.LANCZOS)
    p = derived_path(root, "thumb", sha256)
    p.parent.mkdir(parents=True, exist_ok=True)
    thumb.save(p, "WEBP", quality=80, method=4)
    out["thumb"] = p
    proxy = img.copy()
    proxy.thumbnail((proxy_size, proxy_size), Image.Resampling.LANCZOS)
    p = derived_path(root, "proxy", sha256)
    p.parent.mkdir(parents=True, exist_ok=True)
    proxy.save(p, "JPEG", quality=85, progressive=True, optimize=True)
    out["proxy"] = p
    return out


def video_frame(path: Path, at_s: float = 1.0) -> Image.Image | None:
    """Grab one frame with ffmpeg if installed; None otherwise."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "frame.jpg"
        for ts in (at_s, 0.0):
            try:
                subprocess.run([ffmpeg, "-v", "error", "-ss", str(ts), "-i", str(path), "-frames:v", "1",
                                "-q:v", "3", str(out)], check=True, timeout=60, capture_output=True)
                if out.exists():
                    return Image.open(out).convert("RGB")
            except (subprocess.SubprocessError, OSError):
                continue
    return None


def placeholder(kind: str, size: int = 512) -> Image.Image:
    img = Image.new("RGB", (size, size), (48, 52, 60))
    d = ImageDraw.Draw(img)
    c = size // 2
    if kind == "video":
        r = size // 6
        d.polygon([(c - r // 2, c - r), (c - r // 2, c + r), (c + r, c)], fill=(220, 220, 225))
    else:
        d.rectangle([c - size // 6, c - size // 5, c + size // 6, c + size // 5], outline=(220, 220, 225), width=6)
    return img
