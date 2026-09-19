import itertools
import os

import numpy as np
import pytest

from cloudstore.erasure import gf256 as gf
from cloudstore.erasure import matrix as mx
from cloudstore.erasure.backends import available_backends, get_backend
from cloudstore.erasure.codec import NotEnoughShards, ReedSolomon


def test_field_axioms():
    for a in range(256):
        assert gf.mul(a, 1) == a
        assert gf.mul(a, 0) == 0
        if a:
            assert gf.mul(a, gf.inv(a)) == 1
            assert gf.div(gf.mul(a, 7), 7) == a
    # generator 0x02 has order 255
    seen = {gf.pow_(2, i) for i in range(255)}
    assert len(seen) == 255


def test_distributive_and_commutative():
    rng = np.random.default_rng(1)
    for a, b, c in rng.integers(0, 256, size=(500, 3)):
        a, b, c = int(a), int(b), int(c)
        assert gf.mul(a, b) == gf.mul(b, a)
        assert gf.mul(a, b ^ c) == gf.mul(a, b) ^ gf.mul(a, c)
        assert gf.mul(gf.mul(a, b), c) == gf.mul(a, gf.mul(b, c))


@pytest.mark.parametrize("k,m", [(2, 1), (4, 2), (6, 2), (8, 3), (10, 4)])
def test_every_k_subset_is_invertible(k, m):
    enc = mx.encoding_matrix(k, m)
    subsets = list(itertools.combinations(range(k + m), k))
    for s in subsets[:400]:
        inv = mx.invert([enc[i] for i in s])
        prod = mx.mat_mul(inv, [enc[i] for i in s])
        assert prod == mx.identity(k)


@pytest.mark.parametrize("name", available_backends())
def test_backends_match_reference(name):
    rng = np.random.default_rng(7)
    inputs = [rng.integers(0, 256, 1000, dtype=np.uint8) for _ in range(4)]
    coeffs = mx.cauchy(2, 4)
    ref = get_backend("python").matmul(coeffs, inputs)
    got = get_backend(name).matmul(coeffs, inputs)
    for r, g in zip(ref, got):
        assert np.array_equal(r, g)


@pytest.mark.parametrize("name", available_backends())
@pytest.mark.parametrize("length", [0, 1, 63, 64, 1000, 70001])
def test_rs42_survives_any_two_losses(name, length):
    if name == "python" and length > 2000:
        pytest.skip("reference backend too slow")
    rs = ReedSolomon(4, 2, backend=name)
    data = os.urandom(length)
    shards = rs.encode_bytes(data)
    assert len(shards) == 6
    for lost in itertools.chain(itertools.combinations(range(6), 1), itertools.combinations(range(6), 2)):
        survivors = {i: s for i, s in enumerate(shards) if i not in lost}
        assert rs.decode_bytes(survivors, length) == data
        rebuilt = rs.reconstruct(survivors)
        for i in lost:
            assert np.array_equal(rebuilt[i], shards[i])


def test_three_losses_fail_for_rs42():
    rs = ReedSolomon(4, 2)
    shards = rs.encode_bytes(b"x" * 100)
    with pytest.raises(NotEnoughShards):
        rs.decode_bytes({0: shards[0], 1: shards[1], 2: shards[2]}, 100)


def test_k1_is_replication():
    rs = ReedSolomon(1, 2)
    shards = rs.encode_bytes(b"hello mirror")
    assert all(np.array_equal(shards[0], s) for s in shards)
    assert rs.decode_bytes({2: shards[2]}, 12) == b"hello mirror"
