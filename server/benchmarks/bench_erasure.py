"""Reed–Solomon benchmarks (TDD §12.1–12.3)."""

from __future__ import annotations

import numpy as np

from cloudstore.erasure import matrix as mx
from cloudstore.erasure.backends import available_backends, get_backend
from cloudstore.erasure.codec import ReedSolomon

from .common import MB, best_of, table

PROFILES = [(2, 1), (4, 2), (6, 2), (8, 3)]
DISK_MBPS = 180.0  # sustained sequential throughput of a 4 TB NAS HDD (WD Red Plus / IronWolf class)


def encode_throughput() -> tuple[str, dict]:
    rng = np.random.default_rng(0)
    rows = []
    results = {}
    for name in available_backends():
        be = get_backend(name)
        for k, m in PROFILES:
            shard = 16 * 1024 if name == "python" else 1024 * 1024
            data = [rng.integers(0, 256, shard, dtype=np.uint8) for _ in range(k)]
            rs = ReedSolomon(k, m, backend=be)
            t = best_of(lambda: rs.encode(data), repeat=3 if name == "python" else 5,
                        min_time=0.05 if name == "python" else 0.2)
            mbps = k * shard / t / MB
            results[(be.name, k, m)] = mbps
            rows.append([be.name, f"{k}+{m}", f"{shard // 1024} KiB", mbps])
    # baselines on the fastest backend: single XOR parity (RAID-5) and mirroring (memcpy)
    be = get_backend()
    k = 4
    data = [rng.integers(0, 256, 1024 * 1024, dtype=np.uint8) for _ in range(k)]
    t_xor = best_of(lambda: be.xor_parity(data))
    buf = np.concatenate(data)
    t_mirror = best_of(lambda: buf.copy())
    rows.append([be.name, "4+1 XOR (RAID-5)", "1024 KiB", k * 1024 * 1024 / t_xor / MB])
    rows.append(["memcpy", "mirror (2 copies)", "4096 KiB", len(buf) / t_mirror / MB])
    if hasattr(be, "mul_add_scalar"):
        src = data[0]
        dst = np.zeros_like(src)
        t_scalar = best_of(lambda: be.mul_add_scalar(dst, src, 0x53))
        t_simd = best_of(lambda: be.mul_add(dst, src, 0x53))
        results["simd_speedup"] = t_scalar / t_simd
        rows.append([be.name, "mul_add scalar C", "1024 KiB", len(src) / t_scalar / MB])
        rows.append([be.name, "mul_add SIMD C", "1024 KiB", len(src) / t_simd / MB])
    md = table(["backend", "profile", "shard", "encode MB/s (data in)"], rows)
    return md, results


def reconstruct() -> tuple[str, dict]:
    rng = np.random.default_rng(1)
    be = get_backend()
    k, m = 4, 2
    shard = 4 * 1024 * 1024
    rs = ReedSolomon(k, m, backend=be)
    shards = rs.encode_bytes(rng.integers(0, 256, k * shard, dtype=np.uint8).tobytes(), shard)
    rows = []
    res = {}
    for lost in [(0,), (0, 1), (4,), (1, 5)]:
        survivors = {i: s for i, s in enumerate(shards) if i not in lost}
        rs._inv_cache.clear()
        t = best_of(lambda: rs.reconstruct(survivors, list(lost)), repeat=5)
        mbps = len(lost) * shard / t / MB
        res[lost] = mbps
        kind = "+".join("data" if i < k else "parity" for i in lost)
        rows.append([str(lost), kind, mbps, k * shard / t / MB])
    md = table(["lost shards", "type", "rebuilt MB/s (output)", "survivor MB/s read"], rows)
    # extrapolate: one dead 4 TB disk in 4+2 on 6 disks
    one = res[(0,)]
    disk_bytes = 4e12
    cpu_h = disk_bytes / (one * MB) / 3600
    disk_h = disk_bytes / (DISK_MBPS * MB) / 3600
    md += (f"\n\n**Rebuild of one dead 4 TB disk (4+2, 6 disks) — extrapolated.** The replacement disk must "
           f"receive 4 TB, and each rebuilt shard needs k=4 survivor reads (spread over 5 disks, 3.2 TB each). "
           f"CPU decode at {one:,.0f} MB/s of output would take **{cpu_h:.1f} h**; the new disk's sequential write "
           f"at ~{DISK_MBPS:.0f} MB/s takes **{disk_h:.1f} h**. Rebuild is therefore **disk-bound at ≈{disk_h:.1f} h**, "
           f"with decode using {100 * DISK_MBPS / one:.0f}% of one core. With the default 150 MB/s rebuild throttle "
           f"(to keep foreground reads responsive) the figure is ≈{disk_bytes / 150e6 / 3600:.1f} h.")
    return md, res


def capacity() -> str:
    rows = []
    for n in (2, 4, 6, 8):
        mirror = (0.5, 1)
        raid5 = ((n - 1) / n, 1) if n >= 3 else None
        if n == 2:
            rs = (1, 1)
        elif n == 4:
            rs = (2, 2)
        elif n == 6:
            rs = (4, 2)
        else:
            rs = (6, 2)
        k, m = rs
        rows.append([n, f"{mirror[0]:.0%} / {mirror[1]}",
                     f"{raid5[0]:.0%} / {raid5[1]}" if raid5 else "n/a",
                     f"RS({k},{m}): {k / (k + m):.0%} / {m}"])
    return table(["disks", "2-way mirror: usable / failures survived", "XOR parity (RAID-5)",
                  "Reed–Solomon (this system)"], rows) + (
        "\n\nWith 6 × 4 TB disks: mirroring gives 12 TB and guarantees survival of 1 failure; "
        "RS(4,2) gives **16 TB** and survives **any 2**.")


def cost_matrix_inversion() -> str:
    import time

    rows = []
    for k in (4, 8, 16):
        enc = mx.encoding_matrix(k, 2)
        sub = enc[2 : k + 2]
        t0 = time.perf_counter()
        for _ in range(20):
            mx.invert(sub)
        rows.append([k, (time.perf_counter() - t0) / 20 * 1e6])
    return table(["k", "k×k GF inversion (µs, Python, cached per survivor set)"], rows)
