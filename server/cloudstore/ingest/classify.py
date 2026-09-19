"""Media / non-media classification by magic bytes (TDD §5).

Media = already-compressed image/video formats → erasure-coded store, never
recompressed. Everything else (documents, code, RAW, TIFF, archives…) →
non-media batch store (CDC + zstd).
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import PurePath

RAW_EXTS = {".dng", ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf", ".orf", ".rw2",
            ".pef", ".srw", ".x3f", ".3fr", ".iiq", ".erf", ".kdc", ".mrw"}

HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"}
AVIF_BRANDS = {b"avif", b"avis"}
MP4_BRANDS = {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ", b"M4VP",
              b"dash", b"MSNV", b"XAVC", b"mmp4"}
GPP_BRANDS = {b"3gp4", b"3gp5", b"3gp6", b"3g2a", b"3g2b"}


@dataclass(frozen=True)
class Classification:
    mime: str
    kind: str  # image | video | other
    tier: str  # media | nonmedia


def _media(mime: str, kind: str) -> Classification:
    return Classification(mime, kind, "media")


def _nonmedia(filename: str, fallback: str = "application/octet-stream") -> Classification:
    mime = mimetypes.guess_type(filename)[0] or fallback
    kind = "image" if PurePath(filename).suffix.lower() in RAW_EXTS else "other"
    return Classification(mime, kind, "nonmedia")


def classify(head: bytes, filename: str) -> Classification:
    """Classify from the first >= 64 bytes of the file plus its name."""
    ext = PurePath(filename).suffix.lower()
    if ext in RAW_EXTS:
        return _nonmedia(filename, "image/x-raw")
    if head[:3] == b"\xff\xd8\xff":
        return _media("image/jpeg", "image")
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return _media("image/png", "image")
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return _media("image/gif", "image")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return _media("image/webp", "image")
    if head[4:8] == b"ftyp":
        major = head[8:12]
        # compatible brands follow minor_version; scan the ftyp box for them
        size = int.from_bytes(head[0:4], "big")
        brands = {major} | {head[i : i + 4] for i in range(16, min(size, len(head)) - 3, 4)}
        if major == b"crx ":
            return _nonmedia(filename, "image/x-canon-cr3")
        if brands & AVIF_BRANDS and major in AVIF_BRANDS | {b"mif1", b"msf1"}:
            return _media("image/avif", "image")
        if major in HEIF_BRANDS or (major in (b"mif1", b"msf1") and brands & HEIF_BRANDS):
            return _media("image/heic", "image")
        if major in (b"mif1", b"msf1"):
            return _media("image/heif", "image")
        if major == b"qt  ":
            return _media("video/quicktime", "video")
        if major in GPP_BRANDS:
            return _media("video/3gpp", "video")
        if major in MP4_BRANDS or brands & MP4_BRANDS:
            return _media("video/mp4", "video")
    if ext in (".mov", ".qt") and head[4:8] in (b"moov", b"mdat", b"wide", b"free", b"skip"):
        return _media("video/quicktime", "video")
    return _nonmedia(filename)
