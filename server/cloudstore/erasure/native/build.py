"""Build the native kernel library with the system C compiler.

    python -m cloudstore.erasure.native

Compiles gf_simd.c with -O3 -march=native (x86) / -mcpu=native (arm64) so the
SIMD path matching the deployment CPU is selected at compile time. Build on the
machine that will run the server.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "gf_simd.c"


def lib_path() -> Path:
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    return HERE / f"libcloudstore_native{suffix}"


def build(verbose: bool = True) -> Path:
    cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        raise RuntimeError("no C compiler found (install build-essential / Xcode CLT)")
    machine = platform.machine().lower()
    arch_flag = "-mcpu=native" if machine in ("arm64", "aarch64") else "-march=native"
    out = lib_path()
    shared = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared", "-fPIC"]
    cmd = [cc, "-O3", arch_flag, "-std=c11", "-Wall", "-Wextra", *shared, "-o", str(out), str(SRC)]
    if verbose:
        print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    return out


