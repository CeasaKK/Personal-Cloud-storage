"""Arithmetic over GF(2^8) (TDD §4.1).

Reducing polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11D), generator alpha = 0x02.
Addition is XOR; multiplication goes through log/antilog tables. EXP is doubled
to 512 entries so ``EXP[LOG[a] + LOG[b]]`` never needs a modulo.

This module is the *reference* implementation: scalar operations plus a pure-Python
region kernel. The fast kernels live in ``backends.py`` and ``native/gf_simd.c``
and are tested for byte-identical output against this file.
"""

from __future__ import annotations

POLY = 0x11D
GENERATOR = 0x02


def _build_tables() -> tuple[list[int], list[int]]:
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= POLY
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    # log[0] is undefined; callers must special-case zero.
    return exp, log


EXP, LOG = _build_tables()


def add(a: int, b: int) -> int:
    return a ^ b


sub = add  # characteristic 2: subtraction is addition


def mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError("division by zero in GF(2^8)")
    if a == 0:
        return 0
    return EXP[LOG[a] + 255 - LOG[b]]


def inv(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("zero has no inverse in GF(2^8)")
    return EXP[255 - LOG[a]]


def pow_(a: int, n: int) -> int:
    if n == 0:
        return 1
    if a == 0:
        return 0
    return EXP[(LOG[a] * n) % 255]


def mul_table() -> list[bytes]:
    """Full 256x256 product table; row c is the map x -> c*x as a 256-byte table."""
    return [bytes(mul(c, x) for x in range(256)) for c in range(256)]


MUL_TABLE = mul_table()


def mul_add_region(dst: bytearray, src: bytes | bytearray | memoryview, c: int) -> None:
    """dst ^= c * src, byte-wise. Reference kernel (slow; used by tests)."""
    if len(dst) != len(src):
        raise ValueError("region length mismatch")
    if c == 0:
        return
    if c == 1:
        for i, s in enumerate(src):
            dst[i] ^= s
        return
    row = MUL_TABLE[c]
    for i, s in enumerate(src):
        dst[i] ^= row[s]


def mul_region(dst: bytearray, src: bytes | bytearray | memoryview, c: int) -> None:
    """dst = c * src, byte-wise."""
    row = MUL_TABLE[c]
    dst[:] = bytes(row[s] for s in src)
