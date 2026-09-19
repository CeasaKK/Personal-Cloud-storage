"""Matrices over GF(2^8): Cauchy construction and Gauss–Jordan inversion (TDD §4.2–4.3)."""

from __future__ import annotations

from . import gf256 as gf

Matrix = list[list[int]]


def identity(n: int) -> Matrix:
    return [[1 if i == j else 0 for j in range(n)] for i in range(n)]


def cauchy(m: int, k: int) -> Matrix:
    """m x k Cauchy matrix C[i][j] = 1 / (x_i + y_j), x_i = k + i, y_j = j.

    x and y are disjoint subsets of GF(2^8), so x_i ^ y_j is never zero and every
    square submatrix of C is non-singular. Requires k + m <= 256.
    """
    if k < 1 or m < 0 or k + m > 256:
        raise ValueError(f"invalid code parameters k={k} m={m}")
    return [[gf.inv((k + i) ^ j) for j in range(k)] for i in range(m)]


def encoding_matrix(k: int, m: int) -> Matrix:
    """Systematic (k+m) x k matrix [I_k ; C].

    For k == 1 the parity rows are all ones, which makes the code plain
    replication (the mirror profile, TDD §3) on the same code path.
    """
    if k == 1:
        parity = [[1] for _ in range(m)]
    else:
        parity = cauchy(m, k)
    return identity(k) + parity


def mat_mul(a: Matrix, b: Matrix) -> Matrix:
    rows, inner, cols = len(a), len(b), len(b[0])
    out = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        ai = a[i]
        oi = out[i]
        for t in range(inner):
            c = ai[t]
            if c == 0:
                continue
            bt = b[t]
            for j in range(cols):
                oi[j] ^= gf.mul(c, bt[j])
    return out


def invert(mat: Matrix) -> Matrix:
    """Invert a square matrix over GF(2^8) by Gauss–Jordan elimination.

    Raises ValueError if the matrix is singular.
    """
    n = len(mat)
    aug = [list(row) + [1 if i == j else 0 for j in range(n)] for i, row in enumerate(mat)]
    for col in range(n):
        pivot = next((r for r in range(col, n) if aug[r][col] != 0), None)
        if pivot is None:
            raise ValueError("matrix is singular over GF(2^8)")
        if pivot != col:
            aug[col], aug[pivot] = aug[pivot], aug[col]
        p_inv = gf.inv(aug[col][col])
        row = aug[col]
        for j in range(2 * n):
            row[j] = gf.mul(row[j], p_inv)
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor == 0:
                continue
            rr = aug[r]
            for j in range(2 * n):
                rr[j] ^= gf.mul(factor, row[j])
    return [row[n:] for row in aug]
