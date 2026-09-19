"""CDC dedup and compression: media vs non-media measured separately (TDD §12.7).

The brief records an honesty requirement: on already-compressed media the CDC
gain is expected to be small, and the true number must be reported. Corpora:
  * non-media: real text/code — the Python standard library sources on this
    machine — plus "revised document" copies (a few lines edited per file), the
    realistic dedup case for documents;
  * media: JPEG photos (procedurally generated scenes, encoded like a camera
    would) plus realistic edited copies (re-encoded after a crop / exposure
    tweak), and an optional ``--media-dir`` of real photos.
Exact whole-file duplicates are excluded: those are already caught by SHA-256
content addressing before CDC runs.
"""

from __future__ import annotations

import hashlib
import io
import random
import sysconfig
import time
from pathlib import Path

import numpy as np
import zstandard as zstd
from PIL import Image, ImageEnhance

from cloudstore.dedup.bloom import BloomFilter
from cloudstore.dedup.rabin import Chunker

from .bench_phash import _scene
from .common import MB, table


def nonmedia_corpus(limit_bytes: int = 24_000_000, seed: int = 3, min_size: int = 2000) -> list[bytes]:
    stdlib = Path(sysconfig.get_paths()["stdlib"])
    files = sorted(p for p in stdlib.rglob("*.py") if "site-packages" not in p.parts)
    rng = random.Random(seed)
    out, total = [], 0
    for p in files:
        try:
            b = p.read_bytes()
        except OSError:
            continue
        if len(b) < min_size:
            continue
        out.append(b)
        total += len(b)
        if rng.random() < 0.35:  # a later revision of the same document
            lines = b.split(b"\n")
            for _ in range(rng.randint(1, 3)):
                i = rng.randrange(len(lines))
                lines[i] = lines[i] + b"  # revised"
            rev = b"\n".join(lines)
            out.append(rev)
            total += len(rev)
        if total >= limit_bytes:
            break
    return out


def media_corpus(n: int = 60, media_dir: Path | None = None, seed: int = 4) -> list[bytes]:
    if media_dir:
        return [p.read_bytes() for p in sorted(media_dir.iterdir()) if p.is_file()][:n]
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        img = _scene(rng, 1600, 1200)
        # camera sensor noise: real photos are far less compressible than clean synthetic scenes
        arr = np.asarray(img, dtype=np.float64) + rng.normal(0, 4, (1200, 1600, 3))
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        b = io.BytesIO()
        img.save(b, "JPEG", quality=92)
        out.append(b.getvalue())
        if i % 3 == 0:  # edited copy: small crop + exposure, re-encoded
            e = ImageEnhance.Brightness(img.crop((0, 0, 1580, 1190))).enhance(1.05)
            b = io.BytesIO()
            e.save(b, "JPEG", quality=92)
            out.append(b.getvalue())
    return out


def dedup(corpus: list[bytes], chunker: Chunker) -> dict:
    seen: set[bytes] = set()
    logical = unique = 0
    chunks = 0
    for data in corpus:
        for c in chunker.chunk_bytes(data):
            chunks += 1
            logical += len(c)
            h = hashlib.blake2b(c, digest_size=32).digest()
            if h not in seen:
                seen.add(h)
                unique += len(c)
    return {"logical": logical, "unique": unique, "ratio": logical / unique, "chunks": chunks,
            "avg_chunk": logical / chunks}


def compression(corpus: list[bytes]) -> tuple[float, float]:
    """(per-file zstd ratio, batch zstd ratio with trained dictionary)."""
    raw = sum(len(b) for b in corpus)
    cctx = zstd.ZstdCompressor(level=12)
    per_file = sum(len(cctx.compress(b)) for b in corpus)
    samples = [b[:16384] for b in corpus[:: max(1, len(corpus) // 2000)]]
    try:
        d = zstd.train_dictionary(112 * 1024, samples)
        dctx = zstd.ZstdCompressor(level=12, dict_data=d)
    except zstd.ZstdError:
        dctx, d = cctx, None
    frame, frames, size = bytearray(), 0, len(d.as_bytes()) if d else 0
    for b in corpus:
        frame += b
        if len(frame) >= 4 * 1024 * 1024:
            size += len(dctx.compress(bytes(frame)))
            frame = bytearray()
    if frame:
        size += len(dctx.compress(bytes(frame)))
    return raw / per_file, raw / size


def run(media_dir: Path | None = None) -> tuple[str, dict]:
    chunker = Chunker()  # production parameters: 16 KiB min / 64 KiB avg / 256 KiB max
    nm = nonmedia_corpus()
    big = nonmedia_corpus(min_size=100_000)
    md_ = media_corpus(media_dir=media_dir)
    rows = []
    res = {}
    for name, corpus in (("non-media: all stdlib source + revisions", nm),
                         ("non-media: large docs only (≥100 KB) + revisions", big),
                         ("media: JPEG (sensor noise) + edited copies", md_)):
        d = dedup(corpus, chunker)
        pf, batch = compression(corpus)
        res[name] = (d, pf, batch)
        rows.append([name, len(corpus), d["logical"] / MB, d["unique"] / MB, d["ratio"], d["avg_chunk"] / 1024,
                     pf, batch])
    md = table(["corpus", "files", "logical MB", "unique MB after CDC", "CDC dedup ×", "avg chunk KiB",
                "zstd per-file ×", "zstd batch+dict ×"], rows)
    md += ("\n\nReading the table honestly: **CDC's gain on media is nil** (edited photos are re-encoded, so no "
           "byte ranges survive) and zstd barely touches JPEG — media goes to the erasure-coded tier uncompressed, "
           "as designed. On typical documents most files are smaller than the 16 KiB minimum chunk, so an edit "
           "rewrites the file's only chunk and CDC adds little; CDC pays off on **large** documents with "
           "revisions. For the non-media tier as a whole, the bigger win is **batch compression with a trained "
           "dictionary** versus compressing each file alone.")

    # chunking throughput
    data = np.random.default_rng(0).bytes(64 * 1024 * 1024)
    t = time.perf_counter()
    n = len(chunker.chunk_bytes(data))
    native_mbps = len(data) / (time.perf_counter() - t) / MB
    py = Chunker(use_native=False)
    small = data[: 2 * 1024 * 1024]
    t = time.perf_counter()
    py.chunk_bytes(small)
    py_mbps = len(small) / (time.perf_counter() - t) / MB
    md += (f"\n\nChunking throughput: **{native_mbps:,.0f} MB/s** native C ({chunker.backend}), "
           f"{py_mbps:.1f} MB/s pure-Python reference ({n} chunks over 64 MiB random data).")

    # bloom filter effectiveness on the miss path
    bf = BloomFilter(1_000_000, 0.01)
    present = [hashlib.blake2b(i.to_bytes(8, "little"), digest_size=32).digest() for i in range(1_000_000)]
    for h in present:
        bf.add(h)
    probes = [hashlib.blake2b(b"x" + i.to_bytes(8, "little"), digest_size=32).digest() for i in range(200_000)]
    t = time.perf_counter()
    fp = sum(1 for h in probes if h in bf)
    per = (time.perf_counter() - t) / len(probes) * 1e6
    md += (f"\n\nBloom filter (1 M chunks, {bf.size_bytes / 2**20:.1f} MiB, k={bf.k}): measured false-positive rate "
           f"**{fp / len(probes):.3%}** (target 1%), {per:.1f} µs per probe — i.e. "
           f"{1 - fp / len(probes):.1%} of new-chunk lookups skip the SQLite index entirely.")
    return md, res
