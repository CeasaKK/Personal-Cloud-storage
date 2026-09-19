import hashlib
import io
import os
import random

import pytest

from cloudstore.cache.lru import SizeAwareLRU
from cloudstore.dedup.bloom import BloomFilter
from cloudstore.dedup.rabin import Chunker
from cloudstore.erasure import native
from cloudstore.phash.bktree import BKTree
from cloudstore.phash.mih import MultiIndexHash
from cloudstore.spatial import geohash
from cloudstore.spatial.quadtree import QuadTree
from cloudstore.sync.merkle import EMPTY, MerkleTree, diff


def sha(i: int) -> str:
    return hashlib.sha256(str(i).encode()).hexdigest()


# ---------------------------------------------------------------- merkle
def test_merkle_equal_sets_equal_roots_regardless_of_order():
    a = MerkleTree(sha(i) for i in range(500))
    b = MerkleTree(sha(i) for i in reversed(range(500)))
    assert a.root() == b.root() != EMPTY
    assert MerkleTree().root() == EMPTY


def test_merkle_diff_finds_exact_changes():
    server = MerkleTree(sha(i) for i in range(2000))
    client = MerkleTree(sha(i) for i in range(10, 2005))
    calls = {"children": 0, "buckets": 0}

    def children(paths):
        calls["children"] += 1
        return {p: server.children(p) for p in paths}

    def buckets(paths):
        calls["buckets"] += 1
        return {p: server.bucket(p) for p in paths}

    missing, extra = diff(client, server.root(), children, buckets)
    assert missing == {sha(i) for i in range(2000, 2005)}
    assert extra == {sha(i) for i in range(10)}
    assert calls == {"children": 3, "buckets": 1}


def test_merkle_incremental_matches_rebuild():
    t = MerkleTree(sha(i) for i in range(100))
    t.root()
    t.add(sha(1000))
    t.remove(sha(5))
    fresh = MerkleTree([sha(i) for i in range(100) if i != 5] + [sha(1000)])
    assert t.root() == fresh.root()


# ---------------------------------------------------------------- hamming indexes
def _codes(n, seed=3):
    rng = random.Random(seed)
    base = [rng.getrandbits(64) for _ in range(n // 4)]
    out = []
    for i in range(n):
        b = base[i % len(base)]
        for _ in range(rng.randint(0, 6)):
            b ^= 1 << rng.randrange(64)
        out.append(b)
    return out


@pytest.mark.parametrize("index_cls", [BKTree, MultiIndexHash])
def test_hamming_index_matches_linear_scan(index_cls):
    codes = _codes(3000)
    idx = index_cls()
    for i, c in enumerate(codes):
        idx.add(c, i)
    rng = random.Random(9)
    for _ in range(50):
        q = codes[rng.randrange(len(codes))] ^ (1 << rng.randrange(64))
        for r in (0, 4, 7, 10):
            got = sorted(idx.search(q, r))
            want = sorted((i, (c ^ q).bit_count()) for i, c in enumerate(codes) if (c ^ q).bit_count() <= r)
            assert got == want


def test_bktree_remove():
    t = BKTree()
    t.add(0b1011, 1)
    t.add(0b1011, 2)
    t.add(0b0000, 3)
    assert t.remove(0b1011, 1)
    assert sorted(t.search(0b1011, 0)) == [(2, 0)]


# ---------------------------------------------------------------- CDC + bloom
@pytest.mark.skipif(native.load() is None, reason="native lib not built")
def test_rabin_native_matches_python():
    data = os.urandom(300_000)
    py = Chunker(4096, 12, 32768, use_native=False).chunk_bytes(data)
    c = Chunker(4096, 12, 32768, use_native=True).chunk_bytes(data)
    assert [len(x) for x in py] == [len(x) for x in c]
    assert b"".join(c) == data


def test_cdc_insertion_only_changes_local_chunks():
    ch = Chunker(2048, 11, 16384)
    data = os.urandom(400_000)
    edited = data[:200_000] + b"!" + data[200_000:]
    a = {hashlib.sha256(x).digest() for x in ch.chunk_bytes(data)}
    b = [hashlib.sha256(x).digest() for x in ch.chunk_bytes(edited)]
    changed = sum(1 for x in b if x not in a)
    assert changed <= 2, changed
    assert all(2048 <= len(x) <= 16384 for x in ch.chunk_bytes(data)[:-1])


def test_chunker_stream_matches_bytes():
    ch = Chunker(2048, 11, 16384)
    data = os.urandom(123_457)
    assert list(ch.chunks(io.BytesIO(data), read_size=10_000)) == ch.chunk_bytes(data)


def test_bloom_no_false_negatives_and_fpr(tmp_path):
    bf = BloomFilter(20_000, 0.01)
    items = [hashlib.blake2b(str(i).encode(), digest_size=32).digest() for i in range(20_000)]
    for x in items:
        bf.add(x)
    assert all(x in bf for x in items)
    others = [hashlib.blake2b(f"x{i}".encode(), digest_size=32).digest() for i in range(20_000)]
    fpr = sum(x in bf for x in others) / len(others)
    assert fpr < 0.02
    bf.save(tmp_path / "b.bin")
    loaded = BloomFilter.load(tmp_path / "b.bin")
    assert loaded.count == 20_000 and all(x in loaded for x in items[:100])


# ---------------------------------------------------------------- spatial
def test_geohash_roundtrip_and_covering():
    gh = geohash.encode(28.6139, 77.2090, 9)
    lat_lo, lat_hi, lon_lo, lon_hi = geohash.bbox(gh)
    assert lat_lo <= 28.6139 <= lat_hi and lon_lo <= 77.2090 <= lon_hi
    cells = geohash.covering(28.6139, 77.2090, 5)
    near = geohash.encode(28.64, 77.23, 9)
    assert any(near.startswith(c) for c in cells)


def test_quadtree_clusters_conserve_counts():
    qt = QuadTree()
    rng = random.Random(1)
    pts = [(rng.uniform(-60, 70), rng.uniform(-170, 170)) for _ in range(5000)]
    for i, (la, lo) in enumerate(pts):
        qt.insert(la, lo, i, key=i)
    for zoom in (0, 3, 8, 14):
        cl = qt.clusters(-180, -85, 180, 85, zoom)
        assert sum(c["count"] for c in cl) == 5000
    assert len(qt.clusters(-180, -85, 180, 85, 1)) < len(qt.clusters(-180, -85, 180, 85, 6))
    # viewport restriction
    box = [c for c in qt.clusters(0, 0, 20, 20, 10)]
    want = sum(1 for la, lo in pts if 0 <= la <= 20 and 0 <= lo <= 20)
    assert abs(sum(c["count"] for c in box) - want) <= 20  # edge cells may straddle
    for i in range(100):
        assert qt.remove(*pts[i], i)
    assert sum(c["count"] for c in qt.clusters(-180, -85, 180, 85, 5)) == 4900


def test_size_aware_lru():
    c = SizeAwareLRU(100)
    c.put("a", b"x" * 40)
    c.put("b", b"x" * 40)
    c.get("a")
    c.put("c", b"x" * 40)  # evicts b (least recent)
    assert "a" in c and "c" in c and "b" not in c
    assert c.used == 80
