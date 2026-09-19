"""EXIF / video metadata extraction (TDD §5).

Capture-time priority: EXIF DateTimeOriginal with OffsetTimeOriginal (true
instant) > client-supplied creation date (PHAsset.creationDate) > naive EXIF
time interpreted as UTC > upload time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from PIL import Image, ExifTags

from . import mp4

try:  # HEIC/HEIF/AVIF decoding
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pillow_heif = None

log = logging.getLogger(__name__)

EXIF_IFD = 0x8769
GPS_IFD = 0x8825
SUMMARY_TAGS = {
    "FNumber", "ExposureTime", "ISOSpeedRatings", "PhotographicSensitivity", "FocalLength",
    "FocalLengthIn35mmFilm", "LensModel", "LensMake", "Flash", "WhiteBalance", "ExposureBiasValue",
    "Software", "ImageDescription", "Artist",
}


def _num(v) -> float | None:
    try:
        if isinstance(v, tuple) and len(v) == 2:
            return v[0] / v[1] if v[1] else None
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _dms(values, ref) -> float | None:
    try:
        d, m, s = (_num(x) for x in values)
        if d is None:
            return None
        val = d + (m or 0) / 60 + (s or 0) / 3600
        if isinstance(ref, bytes):
            ref = ref.decode(errors="ignore")
        if ref in ("S", "W"):
            val = -val
        return val
    except (TypeError, ValueError):
        return None


def _parse_exif_time(value: str | None, offset: str | None) -> tuple[float | None, bool]:
    if not value:
        return None, False
    try:
        dt = datetime.strptime(value.strip()[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None, False
    if offset:
        try:
            sign = -1 if offset.strip().startswith("-") else 1
            hh, mm = offset.strip().lstrip("+-").split(":")
            tz = timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
            return dt.replace(tzinfo=tz).timestamp(), True
        except ValueError:
            pass
    return dt.replace(tzinfo=timezone.utc).timestamp(), False


def _jsonable(v):
    if isinstance(v, bytes):
        return None
    if isinstance(v, (tuple, list)):
        return [_jsonable(x) for x in v]
    if isinstance(v, Fraction) or hasattr(v, "numerator"):
        try:
            return round(float(v), 6)
        except (TypeError, ValueError, ZeroDivisionError):
            return str(v)
    if isinstance(v, (int, float, str)):
        return v
    return str(v)


def image_metadata(img: Image.Image) -> dict:
    out: dict = {}
    exif = img.getexif()
    if not exif:
        out["width"], out["height"] = img.size
        return out
    base = {ExifTags.TAGS.get(k, str(k)): v for k, v in exif.items()}
    sub = {ExifTags.TAGS.get(k, str(k)): v for k, v in exif.get_ifd(EXIF_IFD).items()}
    gps = {ExifTags.GPSTAGS.get(k, str(k)): v for k, v in exif.get_ifd(GPS_IFD).items()}
    out["camera_make"] = (str(base.get("Make") or "").strip("\x00 ") or None)
    out["camera_model"] = (str(base.get("Model") or "").strip("\x00 ") or None)
    ts, has_tz = _parse_exif_time(sub.get("DateTimeOriginal") or base.get("DateTime"),
                                  sub.get("OffsetTimeOriginal") or sub.get("OffsetTime"))
    if ts is not None:
        out["exif_ts"], out["exif_has_tz"] = ts, has_tz
    lat = _dms(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef")) if gps.get("GPSLatitude") else None
    lon = _dms(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef")) if gps.get("GPSLongitude") else None
    if lat is not None and lon is not None and (lat, lon) != (0.0, 0.0) and -90 <= lat <= 90 and -180 <= lon <= 180:
        out["lat"], out["lon"] = lat, lon
    orientation = base.get("Orientation", 1)
    w, h = img.size
    if orientation in (5, 6, 7, 8):
        w, h = h, w
    out["width"], out["height"] = w, h
    summary = {k: _jsonable(v) for k, v in {**base, **sub}.items() if k in SUMMARY_TAGS}
    summary["Orientation"] = orientation
    out["exif"] = {k: v for k, v in summary.items() if v is not None}
    return out


def video_metadata(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            meta = mp4.parse(f)
    except Exception as e:  # malformed container: keep ingesting without metadata
        log.warning("mp4 parse failed for %s: %s", path, e)
        return {}
    out: dict = {}
    for k in ("width", "height", "duration", "lat", "lon"):
        if k in meta:
            out[k] = meta[k]
    if "creation_ts" in meta:
        out["exif_ts"] = meta["creation_ts"]
        # mvhd times are UTC by spec; Apple creationdate carries its offset
        out["exif_has_tz"] = True
    if "make" in meta:
        out["camera_make"] = meta["make"]
    if "model" in meta:
        out["camera_model"] = meta["model"]
    return out


def choose_capture_ts(meta: dict, client_ts: float | None, fallback: float) -> float:
    if meta.get("exif_ts") and meta.get("exif_has_tz"):
        return meta["exif_ts"]
    if client_ts:
        return client_ts
    if meta.get("exif_ts"):
        return meta["exif_ts"]
    return fallback
