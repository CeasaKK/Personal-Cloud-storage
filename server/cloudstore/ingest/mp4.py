"""Minimal ISO-BMFF / QuickTime box parser for video metadata (TDD §5).

Reads only box headers and the ``moov`` box (seeking over ``mdat``), so a
multi-gigabyte video costs a few KB of I/O. Extracts:
  * creation time and duration from ``mvhd``
  * display width/height from the video track's ``tkhd``
  * GPS from ``udta/©xyz`` (ISO 6709) or Apple ``meta/keys+ilst``
    (``com.apple.quicktime.location.ISO6709``, ``…creationdate``)
"""

from __future__ import annotations

import re
import struct
from datetime import datetime
from typing import BinaryIO, Iterator

EPOCH_1904 = -2082844800  # seconds from 1904-01-01 to 1970-01-01
CONTAINERS = {b"moov", b"trak", b"mdia", b"udta", b"minf", b"stbl", b"edts"}
ISO6709 = re.compile(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")


def _boxes(f: BinaryIO, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Yield (type, payload_start, payload_end) for boxes in [start, end)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size, btype = struct.unpack(">I4s", hdr)
        header_len = 8
        if size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            header_len = 16
        elif size == 0:
            size = end - pos
        if size < header_len:
            return
        yield btype, pos + header_len, min(pos + size, end)
        pos += size


def _read(f: BinaryIO, start: int, end: int, limit: int = 1 << 20) -> bytes:
    f.seek(start)
    return f.read(min(end - start, limit))


def parse(f: BinaryIO) -> dict:
    f.seek(0, 2)
    file_end = f.tell()
    out: dict = {}
    for btype, s, e in _boxes(f, 0, file_end):
        if btype == b"moov":
            _parse_moov(f, s, e, out)
            break
    return out


def _parse_moov(f: BinaryIO, s: int, e: int, out: dict) -> None:
    for btype, bs, be in _boxes(f, s, e):
        if btype == b"mvhd":
            data = _read(f, bs, be)
            version = data[0]
            if version == 1:
                ctime, _, timescale, duration = struct.unpack(">QQIQ", data[4:32])
            else:
                ctime, _, timescale, duration = struct.unpack(">IIII", data[4:20])
            if ctime:
                out["creation_ts"] = ctime + EPOCH_1904
            if timescale:
                out["duration"] = duration / timescale
        elif btype == b"trak":
            _parse_trak(f, bs, be, out)
        elif btype == b"udta":
            for t, us, ue in _boxes(f, bs, be):
                if t == b"\xa9xyz":
                    raw = _read(f, us, ue)
                    _set_iso6709(raw[4:].decode("utf-8", "ignore"), out)
        elif btype == b"meta":
            _parse_apple_meta(f, bs, be, out)


def _parse_trak(f: BinaryIO, s: int, e: int, out: dict) -> None:
    dims = None
    is_video = False
    for btype, bs, be in _boxes(f, s, e):
        if btype == b"tkhd":
            data = _read(f, bs, be)
            w, h = struct.unpack(">II", data[-8:])
            dims = (w >> 16, h >> 16)
        elif btype == b"mdia":
            for t, ms, me in _boxes(f, bs, be):
                if t == b"hdlr":
                    data = _read(f, ms, me)
                    is_video = data[8:12] == b"vide"
    if is_video and dims and dims[0] and "width" not in out:
        out["width"], out["height"] = dims


def _parse_apple_meta(f: BinaryIO, s: int, e: int, out: dict) -> None:
    keys: list[str] = []
    items: dict[int, bytes] = {}
    for btype, bs, be in _boxes(f, s, e):
        if btype == b"keys":
            data = _read(f, bs, be)
            count = struct.unpack(">I", data[4:8])[0]
            pos = 8
            for _ in range(count):
                if pos + 8 > len(data):
                    break
                ksize = struct.unpack(">I", data[pos : pos + 4])[0]
                keys.append(data[pos + 8 : pos + ksize].decode("utf-8", "ignore"))
                pos += ksize
        elif btype == b"ilst":
            for t, is_, ie in _boxes(f, bs, be):
                idx = struct.unpack(">I", t)[0]
                for dt, ds, de in _boxes(f, is_, ie):
                    if dt == b"data":
                        items[idx] = _read(f, ds, de)[8:]
    for i, name in enumerate(keys, start=1):
        val = items.get(i)
        if val is None:
            continue
        text = val.decode("utf-8", "ignore")
        if name == "com.apple.quicktime.location.ISO6709":
            _set_iso6709(text, out)
        elif name == "com.apple.quicktime.creationdate":
            try:
                out["creation_ts"] = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
                out["creation_has_tz"] = True
            except ValueError:
                pass
        elif name == "com.apple.quicktime.make":
            out["make"] = text
        elif name == "com.apple.quicktime.model":
            out["model"] = text


def _set_iso6709(text: str, out: dict) -> None:
    m = ISO6709.match(text.strip())
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0.0, 0.0):
            out["lat"], out["lon"] = lat, lon
