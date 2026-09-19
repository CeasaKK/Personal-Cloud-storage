"""Perceptual hashes (TDD §9): pHash (DCT) primary, dHash (gradient) secondary.

Both produce 64-bit fingerprints where visually similar images have small
Hamming distance. Inputs are PIL images already rotated per EXIF orientation.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

_N = 32


def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
    m[0, :] = np.sqrt(1.0 / n)
    return m


_DCT = _dct_matrix(_N)


def _bits_to_int(bits: np.ndarray) -> int:
    v = 0
    for b in bits.ravel():
        v = (v << 1) | int(b)
    return v


def phash(img: Image.Image) -> int:
    """32x32 grayscale → 2-D DCT-II → top-left 8x8 without DC → median threshold."""
    g = np.asarray(img.convert("L").resize((_N, _N), Image.Resampling.LANCZOS), dtype=np.float64)
    d = _DCT @ g @ _DCT.T
    block = d[:8, :8].ravel()
    ac = block[1:]
    med = np.median(ac)
    bits = block > med
    bits[0] = False  # DC coefficient carries brightness only
    return _bits_to_int(bits)


def dhash(img: Image.Image) -> int:
    """9x8 grayscale → 64 left/right gradient bits."""
    g = np.asarray(img.convert("L").resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    return _bits_to_int(g[:, 1:] > g[:, :-1])


def sharpness(img: Image.Image) -> float:
    """Variance of the Laplacian on a 512 px grayscale copy (keeper suggestion)."""
    g = img.convert("L")
    g.thumbnail((512, 512))
    a = np.asarray(g, dtype=np.float64)
    if a.shape[0] < 3 or a.shape[1] < 3:
        return 0.0
    lap = -4 * a[1:-1, 1:-1] + a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:]
    return float(lap.var())


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def to_signed(v: int) -> int:
    """Store a 64-bit unsigned hash in SQLite's signed INTEGER."""
    return v - (1 << 64) if v >= (1 << 63) else v


def to_unsigned(v: int) -> int:
    return v + (1 << 64) if v < 0 else v
