import os
import shutil
from pathlib import Path

import pytest

from cloudstore.storage import shardfile as sf
from cloudstore.storage.objectstore import Unrecoverable
from cloudstore.storage.rebuild import RebuildManager
from cloudstore.storage.scrub import Scrubber

from .conftest import Env


def shard_paths(env, key):
    oid = env.store.object_id(key)
    rows = env.db.all(
        "SELECT s.stripe_id, s.idx, d.path FROM shards s JOIN stripes t ON t.id = s.stripe_id "
        "JOIN disks d ON d.id = s.disk_id WHERE t.object_id = ? ORDER BY t.seq, s.idx", (oid,))
    return [(r["stripe_id"], r["idx"], os.path.join(r["path"], sf.shard_relpath(r["stripe_id"], r["idx"]))) for r in rows]


def test_roundtrip_multi_stripe(env):
    data = os.urandom(1_000_003)  # > 3 stripes of 4 x 64 KiB
    env.store.put_bytes("a", data)
    assert env.store.get("a") == data
    assert env.store.read_range("a", 300_000, 50_000) == data[300_000:350_000]
    st = env.db.all("SELECT * FROM stripes")
    assert len(st) == 4 and all((r["k"], r["m"]) == (4, 2) for r in st)


def test_each_shard_on_distinct_disk(env):
    env.store.put_bytes("a", os.urandom(500_000))
    rows = env.db.all("SELECT stripe_id, COUNT(DISTINCT disk_id) AS d, COUNT(*) AS n FROM shards GROUP BY stripe_id")
    assert all(r["d"] == r["n"] == 6 for r in rows)


def test_put_is_idempotent(env):
    a = env.store.put_bytes("x", b"hello")
    b = env.store.put_bytes("x", b"hello")
    assert a == b


def test_empty_object(env):
    env.store.put_bytes("empty", b"")
    assert env.store.get("empty") == b""


def test_degraded_read_two_disks_gone(env):
    data = os.urandom(300_000)
    env.store.put_bytes("a", data)
    shutil.rmtree(env.disk_list[0].path)
    shutil.rmtree(env.disk_list[3].path)
    env.disks.check_all()
    env.store._cache = type(env.store._cache)(0)
    assert env.store.get("a") == data


def test_three_disks_gone_is_unrecoverable(env):
    env.store.put_bytes("a", os.urandom(1000))
    for i in (0, 1, 2):
        shutil.rmtree(env.disk_list[i].path)
    env.disks.check_all()
    with pytest.raises(Unrecoverable):
        env.store.get("a")


def test_bitrot_detected_and_repaired_by_scrub(env):
    data = os.urandom(200_000)
    env.store.put_bytes("a", data)
    paths = shard_paths(env, "a")
    # flip a byte in two shards of the first stripe
    for _, _, p in paths[:2]:
        with open(p, "r+b") as f:
            f.seek(sf.HEADER_SIZE + 10)
            b = f.read(1)
            f.seek(sf.HEADER_SIZE + 10)
            f.write(bytes([b[0] ^ 0xFF]))
    run = Scrubber(env.store, 0, 30).run(full=True)
    assert run["corrupt_found"] == 2 and run["repaired"] == 2 and run["unrecoverable"] == 0
    for sid, idx, p in paths:
        sf.read_shard(Path(p).parents[3], sid, idx)  # verifies checksum
    assert env.store.get("a") == data
    assert env.db.scalar("SELECT COUNT(*) FROM shards WHERE state != 'ok'") == 0


def test_write_with_failed_disk_then_repair(env):
    shutil.rmtree(env.disk_list[5].path)
    env.disks.check_all()
    data = os.urandom(100_000)
    env.store.put_bytes("a", data)  # 5/6 shards land: >= k+1, commits
    assert env.db.scalar("SELECT COUNT(*) FROM shards WHERE state = 'missing'") >= 1
    assert env.store.get("a") == data
    # operator replaces the disk
    rb = RebuildManager(env.store, env.disks, 0)
    tid = rb.replace_disk(env.disk_list[5].id, env.config.data_dir.parent / "disk6", "d6")
    rb.run_task(tid)
    Scrubber(env.store, 0, 30).run(full=True)
    assert env.db.scalar("SELECT COUNT(*) FROM shards WHERE state != 'ok'") == 0
    assert env.store.get("a") == data


def test_replace_failed_disk_rebuilds_all_shards(env):
    blobs = {f"o{i}": os.urandom(50_000 + i * 1000) for i in range(10)}
    for k, v in blobs.items():
        env.store.put_bytes(k, v)
    victim = env.disk_list[2]
    shutil.rmtree(victim.path)
    env.disks.check_all()
    rb = RebuildManager(env.store, env.disks, 0)
    tid = rb.replace_disk(victim.id, env.config.data_dir.parent / "disk-new", "new")
    status = rb.run_task(tid)
    assert status["status"] == "done" and status["done_shards"] == status["total_shards"] > 0
    assert env.db.scalar("SELECT COUNT(*) FROM shards WHERE disk_id = ?", (victim.id,)) == 0
    assert env.disks.get(victim.id).status == "retired"
    env.store._cache = type(env.store._cache)(0)
    for k, v in blobs.items():
        assert env.store.get(k) == v
    # after rebuild the stripe survives two *more* failures
    for i in (0, 1):
        shutil.rmtree(env.disk_list[i].path)
    env.disks.check_all()
    for k, v in blobs.items():
        assert env.store.get(k) == v


def test_mirror_profile_and_restripe(tmp_path):
    e = Env(tmp_path, n_disks=2)
    assert e.store.profile() == (1, 1)
    data = os.urandom(150_000)
    e.store.put_bytes("a", data)
    assert {(r["k"], r["m"]) for r in e.db.all("SELECT k, m FROM stripes")} == {(1, 1)}
    for i in range(2, 6):
        e.disks.add(tmp_path / f"disk{i}", f"d{i}")
    assert e.store.profile() == (4, 2)
    assert e.store.restripe_all() == 1
    assert {(r["k"], r["m"]) for r in e.db.all("SELECT k, m FROM stripes")} == {(4, 2)}
    assert e.store.get("a") == data
    # no stale shard files left behind
    total = sum(1 for d in e.disks.list() for _ in sf.iter_shard_files(d.path))
    assert total == e.db.scalar("SELECT COUNT(*) FROM shards")


def test_crash_recovery_drops_pending(env):
    oid = env.db.execute("INSERT INTO objects(key,size,stripe_count,state,created_at) VALUES('p',0,0,'pending',0)").lastrowid
    env.db.execute("INSERT INTO stripes(id,object_id,seq,k,m,shard_size,data_offset,data_len,state,created_at) "
                   "VALUES('00112233445566778899aabbccddeeff',?,0,4,2,64,0,10,'pending',0)", (oid,))
    d = env.disk_list[0]
    hdr = sf.ShardHeader(0, 4, 2, "00112233445566778899aabbccddeeff", 3, sf.checksum(b"abc"))
    sf.write_shard(d.path, hdr, b"abc", fsync=False)
    assert env.store.recover_pending() == 2
    assert not (d.path / sf.shard_relpath("00112233445566778899aabbccddeeff", 0)).exists()
    assert env.db.scalar("SELECT COUNT(*) FROM objects") == 0


def test_capacity_report(env):
    cap = env.store.capacity()
    assert cap["profile"] == {"k": 4, "m": 2, "mode": "erasure"}
    assert cap["usable_bytes"] == int(cap["raw_bytes"] * 4 / 6)
