"""ctypes binding for the native kernel library (gf_simd.c).

``load()`` returns the library or None if it has not been built. Set
``CLOUDSTORE_NATIVE_AUTOBUILD=1`` to compile on first use.
"""

from __future__ import annotations

import ctypes
import os
from functools import lru_cache

from .build import build, lib_path

_u8p = ctypes.POINTER(ctypes.c_uint8)
_u8pp = ctypes.POINTER(_u8p)


@lru_cache(maxsize=1)
def load():
    path = lib_path()
    if not path.exists() and os.environ.get("CLOUDSTORE_NATIVE_AUTOBUILD") == "1":
        try:
            build(verbose=False)
        except Exception:
            return None
    if not path.exists():
        return None
    lib = ctypes.CDLL(str(path))
    lib.cs_backend_name.restype = ctypes.c_char_p
    lib.cs_gf_mul_add.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint8]
    lib.cs_gf_mul_add.restype = None
    lib.cs_gf_mul_add_scalar.argtypes = lib.cs_gf_mul_add.argtypes
    lib.cs_gf_mul_add_scalar.restype = None
    lib.cs_gf_matmul.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ]
    lib.cs_gf_matmul.restype = None
    lib.cs_xor_parity.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    lib.cs_xor_parity.restype = None
    lib.cs_rabin_tables_size.restype = ctypes.c_size_t
    lib.cs_rabin_init.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    lib.cs_rabin_init.restype = None
    lib.cs_rabin_next.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint64, ctypes.c_int,
    ]
    lib.cs_rabin_next.restype = ctypes.c_size_t
    return lib


def backend_name() -> str | None:
    lib = load()
    return lib.cs_backend_name().decode() if lib else None
