"""Near-duplicate detection benchmarks (TDD §12.5).

Accuracy: a labelled set of groups, each a base scene plus edits that should be
detected as near-duplicates (burst-like brightness shifts, recompression, resize,
small crop, slight rotation, noise), mixed with unrelated scenes. By default the
set is generated procedurally; point ``--labelled-dir`` at a real folder-per-group
set to evaluate on real photos.

Speed: radius query latency for BK-tree vs multi-index hashing vs a vectorised
numpy linear scan, at 10k and 100k codes.
"""

from __future__ import annotations

import io
import random
import time
from itertools import combinations
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance

from cloudstore.phash.bktree import BKTree
from cloudstore.phash.hashing import dhash, phash
from cloudstore.phash.mih import MultiIndexHash

from .common import table


def _scene(rng: np.random.Generator, w: int = 480, h: int = 360) -> Image.Image:
    y, x = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w, 3))
    for c in range(3):
        fx, fy = rng.uniform(0.002, 0.03, 2)
        img[..., c] = 127 + 90 * np.sin(x * fx + y * fy + rng.uniform(0, 6.3))
    for _ in range(rng.integers(3, 9)):
        cx, cy, r = rng.integers(0, w), rng.integers(0, h), rng.integers(15, 110)
        img[(x - cx) ** 2 + (y - cy) ** 2 < r * r] = rng.integers(0, 255, 3)
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))


def _jpeg(img: Image.Image, q: int) -> Image.Image:
    b = io.BytesIO()
    img.save(b, "JPEG", quality=q)
    return Image.open(io.BytesIO(b.getvalue())).convert("RGB")


EDITS = {
    "brightness+8%": lambda im, r: ImageEnhance.Brightness(im).enhance(1.08),
    "recompress q40": lambda im, r: _jpeg(im, 40),
    "resize 50%": lambda im, r: im.resize((im.width // 2, im.height // 2)),
    "crop 4%": lambda im, r: im.crop((int(im.width * .02), int(im.height * .02), int(im.width * .98), int(im.height * .98))),
    "rotate 2deg": lambda im, r: im.rotate(2, resample=Image.Resampling.BILINEAR, expand=False),
    "noise": lambda im, r: Image.fromarray(np.clip(np.asarray(im, dtype=np.float64)
                                                   + r.normal(0, 6, (im.height, im.width, 3)), 0, 255).astype(np.uint8)),
}


def labelled_set(groups: int = 60, seed: int = 11) -> list[tuple[int, Image.Image]]:
    rng = np.random.default_rng(seed)
    items = []
    for g in range(groups):
        base = _scene(rng)
        items.append((g, base))
        for name in rng.choice(list(EDITS), 3, replace=False):
            items.append((g, EDITS[name](base, rng)))
    return items


def load_labelled_dir(root: Path) -> list[tuple[int, Image.Image]]:
    items = []
    for g, d in enumerate(sorted(p for p in root.iterdir() if p.is_dir())):
        for f in sorted(d.iterdir()):
            try:
                items.append((g, Image.open(f).convert("RGB")))
            except OSError:
                continue
    return items


def accuracy(items: list[tuple[int, Image.Image]]) -> tuple[str, dict]:
    labels = [g for g, _ in items]
    hashes = {"pHash": [phash(im) for _, im in items], "dHash": [dhash(im) for _, im in items]}
    pairs = list(combinations(range(len(items)), 2))
    positives = sum(1 for i, j in pairs if labels[i] == labels[j])
    rows = []
    best = {}
    for name, hs in hashes.items():
        dists = [((hs[i] ^ hs[j]).bit_count(), labels[i] == labels[j]) for i, j in pairs]
        for r in (4, 6, 8, 10, 12, 14, 16):
            tp = sum(1 for d, same in dists if d <= r and same)
            fp = sum(1 for d, same in dists if d <= r and not same)
            prec = tp / (tp + fp) if tp + fp else 1.0
            rec = tp / positives
            f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0
            rows.append([name, r, prec, rec, f1])
            if f1 > best.get(name, (0, 0))[1]:
                best[name] = (r, f1, prec, rec)
    md = table(["hash", "radius", "precision", "recall", "F1"], rows)
    return md, best


def _codes(n: int, seed: int = 5) -> list[int]:
    """Clustered code distribution: ~n/4 scenes with a few near-duplicate variants each."""
    rng = random.Random(seed)
    bases = [rng.getrandbits(64) for _ in range(max(1, n // 4))]
    out = []
    for i in range(n):
        b = bases[i % len(bases)]
        for _ in range(rng.randint(0, 5)):
            b ^= 1 << rng.randrange(64)
        out.append(b)
    return out


def _popcount64(x: np.ndarray) -> np.ndarray:
    x = x - ((x >> np.uint64(1)) & np.uint64(0x5555555555555555))
    x = (x & np.uint64(0x3333333333333333)) + ((x >> np.uint64(2)) & np.uint64(0x3333333333333333))
    x = (x + (x >> np.uint64(4))) & np.uint64(0x0F0F0F0F0F0F0F0F)
    return (x * np.uint64(0x0101010101010101)) >> np.uint64(56)


def speed(radius: int = 10, queries: int = 300) -> str:
    rows = []
    for n in (10_000, 100_000):
        codes = _codes(n)
        bk = BKTree()
        mih = MultiIndexHash()
        t0 = time.perf_counter()
        for i, c in enumerate(codes):
            bk.add(c, i)
        bk_build = time.perf_counter() - t0
        for i, c in enumerate(codes):
            mih.add(c, i)
        arr = np.array(codes, dtype=np.uint64)
        rng = random.Random(1)
        qs = [codes[rng.randrange(n)] ^ (1 << rng.randrange(64)) for _ in range(queries)]

        def per_query(fn) -> float:
            t = time.perf_counter()
            for q in qs:
                fn(q)
            return (time.perf_counter() - t) / queries * 1e3

        bk.nodes_visited = 0
        t_bk = per_query(lambda q: bk.search(q, radius))
        visited = bk.nodes_visited / queries
        t_mih = per_query(lambda q: mih.search(q, radius))
        t_mih7 = per_query(lambda q: mih.search(q, 7))
        t_np = per_query(lambda q: np.nonzero(_popcount64(arr ^ np.uint64(q)) <= radius)[0])
        t_py = per_query(lambda q: [i for i, c in enumerate(codes) if (c ^ q).bit_count() <= radius]) \
            if n <= 10_000 else float("nan")
        rows.append([f"{n:,}", t_bk, f"{visited / n:.1%}", t_mih, t_mih7, t_np, t_py, bk_build])
    return table(["codes", f"BK-tree r={radius} ms", "BK nodes visited", f"MIH r={radius} ms", "MIH r=7 ms",
                  "numpy linear ms", "Python linear ms", "BK build s"], rows)
