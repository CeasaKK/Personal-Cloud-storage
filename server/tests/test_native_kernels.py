"""Compile and run the C kernel self-test for every SIMD path the host can execute
(host ISA, plus x86 AVX2 / SSSE3 / scalar under Rosetta 2 on Apple silicon)."""

import platform
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "cloudstore" / "erasure" / "native" / "gf_simd.c"
TEST = Path(__file__).with_name("native") / "kernel_selftest.c"
CC = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")


def variants():
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        out = [("neon", ["-mcpu=native"], [])]
        if platform.system() == "Darwin" and subprocess.run(["arch", "-x86_64", "/usr/bin/true"],
                                                            capture_output=True).returncode == 0:
            for name, flag in (("avx2", "-mavx2"), ("ssse3", "-mssse3"), ("scalar", "-mno-sse3")):
                out.append((f"x86-{name}", ["-target", "x86_64-apple-macos12", flag], ["arch", "-x86_64"]))
        return out
    return [("native", ["-march=native"], []), ("ssse3", ["-mssse3", "-mno-avx2"], []), ("scalar", ["-mno-sse3"], [])]


@pytest.mark.skipif(CC is None, reason="no C compiler")
@pytest.mark.parametrize("name,flags,runner", variants(), ids=[v[0] for v in variants()])
def test_simd_kernels_match_scalar(tmp_path, name, flags, runner):
    exe = tmp_path / f"selftest-{name}"
    subprocess.run([CC, "-O3", "-std=c11", *flags, str(SRC), str(TEST), "-o", str(exe)], check=True)
    r = subprocess.run([*runner, str(exe)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all kernels match" in r.stdout
