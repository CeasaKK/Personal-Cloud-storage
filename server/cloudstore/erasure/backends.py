"""Region-kernel backends for GF(2^8) matrix × shard products (TDD §4.4).

All backends implement ``matmul(coeffs, inputs) -> outputs`` where ``coeffs``
is a rows×cols matrix of field elements, ``inputs`` is ``cols`` equal-length
uint8 numpy arrays and the result is ``rows`` uint8 arrays with
``out[r] = XOR_c coeffs[r][c] * inputs[c]``. Encode and decode are both this
one operation with different matrices.
"""

from __future__ import annotations

import ctypes
import os
from typing import Sequence

import numpy as np

from . import gf256
from . import native as _native


class Backend:
    name = "abstract"

    def matmul(self, coeffs: Sequence[Sequence[int]], inputs: Sequence[np.ndarray]) -> list[np.ndarray]:
        raise NotImplementedError

    def xor_parity(self, inputs: Sequence[np.ndarray]) -> np.ndarray:
        out = np.zeros_like(inputs[0])
        for a in inputs:
            np.bitwise_xor(out, a, out=out)
        return out


class PythonBackend(Backend):
    """Pure-Python byte loop. Reference only — tens of KB/s."""

    name = "python"

    def matmul(self, coeffs, inputs):
        n = len(inputs[0])
        outs = []
        srcs = [bytes(a) for a in inputs]
        for row in coeffs:
            dst = bytearray(n)
            for c, src in zip(row, srcs):
                gf256.mul_add_region(dst, src, c)
            outs.append(np.frombuffer(bytes(dst), dtype=np.uint8).copy())
        return outs


class NumpyBackend(Backend):
    """64 KiB product table + fancy indexing. Vectorised, no compiler needed."""

    name = "numpy"

    def __init__(self) -> None:
        self._table = np.array([list(r) for r in gf256.MUL_TABLE], dtype=np.uint8)

    def matmul(self, coeffs, inputs):
        outs = []
        for row in coeffs:
            acc = np.zeros_like(inputs[0])
            for c, src in zip(row, inputs):
                if c == 0:
                    continue
                if c == 1:
                    np.bitwise_xor(acc, src, out=acc)
                else:
                    np.bitwise_xor(acc, self._table[c][src], out=acc)
            outs.append(acc)
        return outs


class NativeBackend(Backend):
    """C split-nibble kernel with NEON / AVX2 / SSSE3 table lookups."""

    def __init__(self, lib) -> None:
        self._lib = lib
        self.name = f"native-{lib.cs_backend_name().decode()}"

    @staticmethod
    def _ptr_array(arrays: Sequence[np.ndarray]):
        arr = (ctypes.c_void_p * len(arrays))()
        for i, a in enumerate(arrays):
            arr[i] = a.ctypes.data
        return arr

    def matmul(self, coeffs, inputs):
        rows, cols = len(coeffs), len(inputs)
        n = len(inputs[0])
        inputs = [np.ascontiguousarray(a, dtype=np.uint8) for a in inputs]
        outs = [np.empty(n, dtype=np.uint8) for _ in range(rows)]
        flat = (ctypes.c_uint8 * (rows * cols))(*[c for row in coeffs for c in row])
        self._lib.cs_gf_matmul(
            ctypes.cast(flat, ctypes.c_void_p), rows, cols,
            ctypes.cast(self._ptr_array(inputs), ctypes.c_void_p),
            ctypes.cast(self._ptr_array(outs), ctypes.c_void_p), n,
        )
        return outs

    def xor_parity(self, inputs):
        inputs = [np.ascontiguousarray(a, dtype=np.uint8) for a in inputs]
        out = np.empty(len(inputs[0]), dtype=np.uint8)
        self._lib.cs_xor_parity(
            ctypes.cast(self._ptr_array(inputs), ctypes.c_void_p), len(inputs),
            out.ctypes.data, len(out),
        )
        return out

    def mul_add_scalar(self, dst: np.ndarray, src: np.ndarray, c: int) -> None:
        self._lib.cs_gf_mul_add_scalar(dst.ctypes.data, src.ctypes.data, len(dst), c)

    def mul_add(self, dst: np.ndarray, src: np.ndarray, c: int) -> None:
        self._lib.cs_gf_mul_add(dst.ctypes.data, src.ctypes.data, len(dst), c)


_cache: dict[str, Backend] = {}


def get_backend(name: str | None = None) -> Backend:
    """Return a backend by name ('native' | 'numpy' | 'python'), or the best available."""
    name = name or os.environ.get("CLOUDSTORE_GF_BACKEND") or "auto"
    if name in _cache:
        return _cache[name]
    backend: Backend
    if name == "python":
        backend = PythonBackend()
    elif name == "numpy":
        backend = NumpyBackend()
    elif name == "native":
        lib = _native.load()
        if lib is None:
            raise RuntimeError("native backend not built: python -m cloudstore.erasure.native")
        backend = NativeBackend(lib)
    elif name == "auto":
        lib = _native.load()
        backend = NativeBackend(lib) if lib is not None else NumpyBackend()
    else:
        raise ValueError(f"unknown GF backend {name!r}")
    _cache[name] = backend
    return backend


def available_backends() -> list[str]:
    names = ["python", "numpy"]
    if _native.load() is not None:
        names.append("native")
    return names
