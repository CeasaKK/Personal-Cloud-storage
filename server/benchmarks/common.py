"""Shared benchmark helpers: timing, machine description, markdown tables."""

from __future__ import annotations

import os
import platform
import statistics
import subprocess
import time
from typing import Callable


def machine() -> str:
    cpu = platform.processor() or platform.machine()
    try:
        if platform.system() == "Darwin":
            cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
        elif os.path.exists("/proc/cpuinfo"):
            for line in open("/proc/cpuinfo"):
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return f"{cpu}, {os.cpu_count()} cores, {platform.system()} {platform.release()}, Python {platform.python_version()}"


def best_of(fn: Callable[[], object], repeat: int = 5, min_time: float = 0.2) -> float:
    """Best wall time of ``fn`` (seconds), looping each sample to >= min_time."""
    best = float("inf")
    for _ in range(repeat):
        n = 0
        t0 = time.perf_counter()
        while True:
            fn()
            n += 1
            el = time.perf_counter() - t0
            if el >= min_time:
                break
        best = min(best, el / n)
    return best


def percentiles(samples: list[float]) -> tuple[float, float, float]:
    s = sorted(samples)
    q = statistics.quantiles(s, n=100) if len(s) >= 2 else [s[0]] * 99
    return s[len(s) // 2], q[94], q[98]


def table(headers: list[str], rows: list[list]) -> str:
    def fmt(v):
        if isinstance(v, float):
            return f"{v:,.2f}" if abs(v) < 1000 else f"{v:,.0f}"
        return str(v)
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(fmt(v) for v in r) + " |" for r in rows]
    return "\n".join(out)


MB = 1e6
