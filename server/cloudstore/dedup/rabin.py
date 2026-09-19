"""Content-defined chunking with a Rabin fingerprint (TDD §6).

A 64-byte window slides over the data; the fingerprint is the window's
polynomial remainder modulo an irreducible degree-53 polynomial over GF(2).
A chunk boundary is placed where ``fp & mask == 0``, so boundaries depend on
content, not position: inserting a byte only changes the chunk containing it.

The C implementation in ``erasure/native/gf_simd.c`` is used when built; the
pure-Python version here is the reference and produces identical boundaries.
"""

from __future__ import annotations

import ctypes
from typing import BinaryIO, Iterator

from ..erasure import native

POLYNOMIAL = 0x3DA3358B4DC173  # irreducible, degree 53 (restic's default)
WINDOW = 64


def _deg(p: int) -> int:
    return p.bit_length() - 1


def _mod(x: int, p: int) -> int:
    dp = _deg(p)
    while x and _deg(x) >= dp:
        x ^= p << (_deg(x) - dp)
    return x


def _append_byte(h: int, b: int, pol: int) -> int:
    return _mod((h << 8) | b, pol)


class RabinTables:
    def __init__(self, pol: int = POLYNOMIAL) -> None:
        self.pol = pol
        k = _deg(pol)
        self.shift = k - 8
        self.out_table = []
        self.mod_table = []
        for b in range(256):
            h = _append_byte(0, b, pol)
            for _ in range(WINDOW - 1):
                h = _append_byte(h, 0, pol)
            self.out_table.append(h)
            hb = b << k
            self.mod_table.append(_mod(hb, pol) | hb)


_TABLES: RabinTables | None = None


def tables() -> RabinTables:
    global _TABLES
    if _TABLES is None:
        _TABLES = RabinTables()
    return _TABLES


def next_boundary_py(buf: bytes, min_size: int, max_size: int, mask: int, eof: bool) -> int:
    """Reference implementation mirroring ``cs_rabin_next`` in C."""
    n = len(buf)
    if n <= min_size:
        return n if eof else 0
    t = tables()
    out_t, mod_t, shift = t.out_table, t.mod_table, t.shift
    limit = min(n, max_size)
    window = [0] * WINDOW
    wpos = 0
    digest = 0
    start = max(min_size - WINDOW, 0)
    mask64 = (1 << 64) - 1
    for i in range(start, limit):
        b = buf[i]
        out = window[wpos]
        window[wpos] = b
        wpos = (wpos + 1) % WINDOW
        digest ^= out_t[out]
        index = digest >> shift
        digest = ((digest << 8) | b) & mask64
        digest ^= mod_t[index & 0xFF]
        if i + 1 >= min_size and (digest & mask) == 0:
            return i + 1
    if limit == max_size:
        return max_size
    return n if eof else 0


class Chunker:
    def __init__(self, min_size: int = 16 * 1024, avg_bits: int = 16, max_size: int = 256 * 1024,
                 use_native: bool | None = None) -> None:
        if not (0 < min_size < max_size):
            raise ValueError("need 0 < min_size < max_size")
        self.min_size = min_size
        self.max_size = max_size
        self.mask = (1 << avg_bits) - 1
        lib = native.load() if use_native in (None, True) else None
        if use_native and lib is None:
            raise RuntimeError("native library not built")
        self._lib = lib
        if lib is not None:
            self._tables = ctypes.create_string_buffer(lib.cs_rabin_tables_size())
            lib.cs_rabin_init(self._tables, POLYNOMIAL)

    @property
    def backend(self) -> str:
        return "native" if self._lib is not None else "python"

    def _boundary(self, buf: bytearray, pos: int, eof: bool) -> int:
        n = len(buf) - pos
        if self._lib is None:
            return next_boundary_py(memoryview(buf)[pos:], self.min_size, self.max_size, self.mask, eof)
        ptr = ctypes.addressof(ctypes.c_char.from_buffer(buf, pos))
        return self._lib.cs_rabin_next(self._tables, ptr, n, self.min_size, self.max_size, self.mask, int(eof))

    def chunks(self, f: BinaryIO, read_size: int = 4 * 1024 * 1024) -> Iterator[bytes]:
        """Yield content-defined chunks of a binary stream."""
        read_size = max(read_size, 2 * self.max_size)
        buf = bytearray()
        pos = 0
        eof = False
        while True:
            if not eof and len(buf) - pos < self.max_size:
                del buf[:pos]
                pos = 0
                while not eof and len(buf) < read_size:
                    more = f.read(read_size - len(buf))
                    if not more:
                        eof = True
                    else:
                        buf += more
            if pos >= len(buf):
                return
            cut = self._boundary(buf, pos, eof)
            yield bytes(buf[pos : pos + cut])
            pos += cut

    def chunk_bytes(self, data: bytes) -> list[bytes]:
        import io

        return list(self.chunks(io.BytesIO(data)))
