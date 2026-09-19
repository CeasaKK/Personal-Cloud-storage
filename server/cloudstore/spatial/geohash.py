"""Geohash encode/decode + neighbours (TDD §10): SQL prefix pre-filter for 'near' search."""

from __future__ import annotations

import math

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_DECODE = {c: i for i, c in enumerate(_BASE32)}


def encode(lat: float, lon: float, precision: int = 9) -> str:
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    out, bits, ch, even = [], 0, 0, True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch = (ch << 1) | 1
                lon_lo = mid
            else:
                ch <<= 1
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch = (ch << 1) | 1
                lat_lo = mid
            else:
                ch <<= 1
                lat_hi = mid
        even = not even
        bits += 1
        if bits == 5:
            out.append(_BASE32[ch])
            bits, ch = 0, 0
    return "".join(out)


def bbox(gh: str) -> tuple[float, float, float, float]:
    """(lat_lo, lat_hi, lon_lo, lon_hi) of a geohash cell."""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    even = True
    for c in gh:
        v = _DECODE[c]
        for shift in range(4, -1, -1):
            bit = (v >> shift) & 1
            if even:
                mid = (lon_lo + lon_hi) / 2
                lon_lo, lon_hi = (mid, lon_hi) if bit else (lon_lo, mid)
            else:
                mid = (lat_lo + lat_hi) / 2
                lat_lo, lat_hi = (mid, lat_hi) if bit else (lat_lo, mid)
            even = not even
    return lat_lo, lat_hi, lon_lo, lon_hi


def cell_size_km(precision: int) -> tuple[float, float]:
    """Approximate (height, width) of a cell at the equator in km."""
    lat_bits = (precision * 5) // 2
    lon_bits = precision * 5 - lat_bits
    return 180 / (1 << lat_bits) * 111.32, 360 / (1 << lon_bits) * 111.32


def covering(lat: float, lon: float, radius_km: float) -> list[str]:
    """Geohash prefixes whose union covers the circle: the centre cell and its
    8 neighbours at the finest precision whose cell is >= the radius."""
    precision = 1
    for p in range(9, 0, -1):
        h, w = cell_size_km(p)
        w *= max(math.cos(math.radians(lat)), 0.01)
        if h >= radius_km and w >= radius_km:
            precision = p
            break
    center = encode(lat, lon, precision)
    lat_lo, lat_hi, lon_lo, lon_hi = bbox(center)
    dlat, dlon = lat_hi - lat_lo, lon_hi - lon_lo
    cells = set()
    for i in (-1, 0, 1):
        for j in (-1, 0, 1):
            la = min(max(lat + i * dlat, -89.999999), 89.999999)
            lo = ((lon + j * dlon + 180) % 360) - 180
            cells.add(encode(la, lo, precision))
    return sorted(cells)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
