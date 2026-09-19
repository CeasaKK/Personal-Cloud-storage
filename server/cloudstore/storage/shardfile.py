"""Self-describing shard files (TDD §4.6) with atomic writes (TDD §4.7).

Layout: 256-byte header followed by the payload. The header carries enough to
rebuild the object/stripe/shard index from the disks alone (``recovery.py``):

    0   4  magic "CSHD"          24  8  payload length
    4   1  version               32 32  BLAKE2b-256 of payload
    5   1  shard index           64  4  stripe seq within object
    6   1  k                     68  8  stripe data offset in object
    7   1  m                     76  8  stripe data length
    8  16  stripe uuid           84  2  object key length, 86.. key (<=166 B)
                                252  4  CRC32 of header bytes 0..251
"""

from __future__ import annotations

import hashlib
import os
import struct
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"CSHD"
VERSION = 1
HEADER = struct.Struct("<4sBBBB16sQ32sIQQH")
HEADER_SIZE = 256
MAX_KEY = HEADER_SIZE - HEADER.size - 4


class ShardError(Exception):
    pass


class MissingShard(ShardError):
    pass


class CorruptShard(ShardError):
    pass


class DiskIOError(ShardError):
    """The disk itself failed the operation (not a checksum problem)."""


@dataclass(frozen=True)
class ShardHeader:
    index: int
    k: int
    m: int
    stripe_id: str
    length: int
    checksum: bytes
    seq: int = 0
    data_offset: int = 0
    data_len: int = 0
    key: str = ""


def checksum(payload) -> bytes:
    return hashlib.blake2b(payload, digest_size=32).digest()


def shard_relpath(stripe_id: str, index: int) -> str:
    return f"shards/{stripe_id[0:2]}/{stripe_id[2:4]}/{stripe_id}.{index}.shd"


def pack_header(h: ShardHeader) -> bytes:
    key = h.key.encode()
    if len(key) > MAX_KEY:
        raise ValueError("object key too long for shard header")
    body = HEADER.pack(MAGIC, VERSION, h.index, h.k, h.m, uuid.UUID(h.stripe_id).bytes, h.length, h.checksum,
                       h.seq, h.data_offset, h.data_len, len(key)) + key
    body = body.ljust(HEADER_SIZE - 4, b"\0")
    return body + struct.pack("<I", zlib.crc32(body))


def unpack_header(raw: bytes) -> ShardHeader:
    if len(raw) < HEADER_SIZE:
        raise CorruptShard("truncated header")
    body = raw[: HEADER_SIZE - 4]
    (crc,) = struct.unpack("<I", raw[HEADER_SIZE - 4 : HEADER_SIZE])
    if zlib.crc32(body) != crc:
        raise CorruptShard("header CRC mismatch")
    magic, version, index, k, m, sid, length, csum, seq, off, dlen, klen = HEADER.unpack(body[: HEADER.size])
    if magic != MAGIC or version != VERSION:
        raise CorruptShard("bad magic/version")
    key = body[HEADER.size : HEADER.size + klen].decode()
    return ShardHeader(index, k, m, uuid.UUID(bytes=sid).hex, length, csum, seq, off, dlen, key)


def read_header(path: Path) -> ShardHeader:
    with open(path, "rb") as f:
        return unpack_header(f.read(HEADER_SIZE))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_shard(disk_root: Path, header: ShardHeader, payload, fsync: bool = True) -> None:
    """tmp + fsync + rename + fsync(dir). Raises DiskIOError on any OS failure."""
    final = disk_root / shard_relpath(header.stripe_id, header.index)
    tmp = disk_root / "tmp" / f"{header.stripe_id}.{header.index}.part"
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(pack_header(header))
            f.write(payload)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, final)
        if fsync:
            _fsync_dir(final.parent)
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise DiskIOError(str(e)) from e


def read_shard(disk_root: Path, stripe_id: str, index: int, expected: bytes | None = None) -> tuple[ShardHeader, bytes]:
    """Read and verify a shard. Raises MissingShard / CorruptShard / DiskIOError."""
    path = disk_root / shard_relpath(stripe_id, index)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError as e:
        raise MissingShard(str(path)) from e
    except OSError as e:
        raise DiskIOError(str(e)) from e
    header = unpack_header(raw)
    payload = raw[HEADER_SIZE:]
    if header.stripe_id != stripe_id or header.index != index:
        raise CorruptShard("header identity mismatch")
    if len(payload) != header.length:
        raise CorruptShard("length mismatch")
    actual = checksum(payload)
    if actual != header.checksum or (expected is not None and actual != expected):
        raise CorruptShard(f"checksum mismatch in {path.name}")
    return header, payload


def delete_shard(disk_root: Path, stripe_id: str, index: int) -> None:
    try:
        (disk_root / shard_relpath(stripe_id, index)).unlink(missing_ok=True)
    except OSError:
        pass


def iter_shard_files(disk_root: Path):
    """Yield every shard path on a disk (used by index recovery)."""
    base = disk_root / "shards"
    if base.exists():
        yield from base.glob("*/*/*.shd")
