"""Thumbnail cache hit rate vs budget on simulated scroll traces (TDD §12.8).

Trace model (from how people browse a photo timeline): mostly forward scrolling
through the recent months, frequent short back-scrolls to re-look at a burst,
occasional jumps to an older month, and repeated sessions that start again at the
top. Thumbnail sizes follow the WebP 256 px distribution (~8–30 KB).
"""

from __future__ import annotations

import random

from cloudstore.cache.lru import SizeAwareLRU

from .common import table

PAGE = 60  # thumbnails visible per screen


def trace(n_items: int = 20_000, sessions: int = 40, seed: int = 2) -> list[int]:
    rng = random.Random(seed)
    out = []
    for _ in range(sessions):
        pos = 0
        for _ in range(rng.randint(10, 80)):
            r = rng.random()
            if r < 0.70:
                pos += PAGE // 2
            elif r < 0.90:
                pos = max(0, pos - rng.randint(PAGE // 2, 3 * PAGE))
            else:
                pos = min(n_items - PAGE, pos + rng.randint(500, 5000))
            pos = min(pos, n_items - PAGE)
            out.extend(range(pos, pos + PAGE))
    return out


def run() -> str:
    rng = random.Random(7)
    sizes = {i: rng.randint(8_000, 30_000) for i in range(20_000)}
    tr = trace()
    rows = []
    for budget_mb in (16, 32, 64, 128, 256):
        lru = SizeAwareLRU(budget_mb * 1_000_000)
        pref = SizeAwareLRU(budget_mb * 1_000_000)
        pref_loads = 0
        for item in tr:
            if lru.get(item) is None:
                lru.put(item, b"\0" * sizes[item])
            if pref.get(item) is None:
                pref.put(item, b"\0" * sizes[item])
                pref_loads += 1
                # prefetch the next half-page on a miss (loaded off the request path)
                for j in range(item + 1, min(item + PAGE // 2, 20_000)):
                    if j not in pref:
                        pref.put(j, b"\0" * sizes[j])
                        pref_loads += 1
        rows.append([budget_mb, lru.stats()["hit_rate"], lru.misses, pref.stats()["hit_rate"], pref_loads])
    return table(["budget MB", "LRU hit rate", "LRU SSD loads", "LRU+prefetch hit rate", "LRU+prefetch SSD loads"],
                 rows) + (
        "\n\nPrefetch converts request-path misses into background loads: the hit rate rises because the next "
        "half-page is already warm, while total SSD loads stay similar (the column on the right)."
        f"\n\nTrace: {len(tr):,} thumbnail requests over 20,000 items. The default budget (256 MB) holds the working "
        "set of a heavy browsing session; the server-side cache sits behind the browser's own HTTP cache "
        "(thumbnails are served `immutable`), so it mostly absorbs cross-device and post-restart traffic.")
