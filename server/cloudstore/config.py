"""Runtime configuration.

Values come from environment variables prefixed ``CLOUDSTORE_`` (see
``deploy/cloudstore.env.example``). ``data_dir`` is on the system SSD and holds
the SQLite metadata, tus staging, derived thumbnails and the Bloom filter;
original bytes only ever live on the registered data disks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

MiB = 1024 * 1024
GiB = 1024 * MiB


def _env(name: str, default, cast=str):
    raw = os.environ.get(f"CLOUDSTORE_{name}")
    if raw is None:
        return default
    if cast is bool:
        return raw.lower() in ("1", "true", "yes", "on")
    return cast(raw)


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")).resolve())

    # Erasure coding (TDD §3, §4)
    ec_k: int = field(default_factory=lambda: _env("EC_K", 4, int))
    ec_m: int = field(default_factory=lambda: _env("EC_M", 2, int))
    max_shard_size: int = field(default_factory=lambda: _env("MAX_SHARD_SIZE", 4 * MiB, int))
    # minimum shards that must land for a write to commit; default k+1
    min_write_extra: int = field(default_factory=lambda: _env("MIN_WRITE_EXTRA", 1, int))
    stripe_cache_bytes: int = field(default_factory=lambda: _env("STRIPE_CACHE_BYTES", 128 * MiB, int))
    fsync: bool = field(default_factory=lambda: _env("FSYNC", True, bool))

    # Scrub / health / rebuild (TDD §4.9–4.10)
    scrub_rate_bytes: int = field(default_factory=lambda: _env("SCRUB_RATE_BYTES", 50 * MiB, int))
    scrub_interval_days: float = field(default_factory=lambda: _env("SCRUB_INTERVAL_DAYS", 30.0, float))
    rebuild_rate_bytes: int = field(default_factory=lambda: _env("REBUILD_RATE_BYTES", 150 * MiB, int))
    health_interval_s: float = field(default_factory=lambda: _env("HEALTH_INTERVAL_S", 60.0, float))
    disk_error_threshold: int = field(default_factory=lambda: _env("DISK_ERROR_THRESHOLD", 10, int))

    # Non-media tier (TDD §6)
    batch_threshold: int = field(default_factory=lambda: _env("BATCH_THRESHOLD", 1 * GiB, int))
    batch_max_age_s: float = field(default_factory=lambda: _env("BATCH_MAX_AGE_S", 6 * 3600.0, float))
    batch_frame_size: int = field(default_factory=lambda: _env("BATCH_FRAME_SIZE", 4 * MiB, int))
    batch_compact_ratio: float = field(default_factory=lambda: _env("BATCH_COMPACT_RATIO", 0.5, float))
    cdc_min: int = field(default_factory=lambda: _env("CDC_MIN", 16 * 1024, int))
    cdc_avg_bits: int = field(default_factory=lambda: _env("CDC_AVG_BITS", 16, int))
    cdc_max: int = field(default_factory=lambda: _env("CDC_MAX", 256 * 1024, int))
    bloom_capacity: int = field(default_factory=lambda: _env("BLOOM_CAPACITY", 10_000_000, int))
    bloom_fpr: float = field(default_factory=lambda: _env("BLOOM_FPR", 0.01, float))

    # Media / browse
    thumb_size: int = field(default_factory=lambda: _env("THUMB_SIZE", 256, int))
    proxy_size: int = field(default_factory=lambda: _env("PROXY_SIZE", 1600, int))
    thumb_cache_bytes: int = field(default_factory=lambda: _env("THUMB_CACHE_BYTES", 256 * MiB, int))
    dup_radius: int = field(default_factory=lambda: _env("DUP_RADIUS", 10, int))
    trash_retention_days: float = field(default_factory=lambda: _env("TRASH_RETENTION_DAYS", 30.0, float))

    # Upload / auth (TDD §8, §11)
    max_upload_size: int = field(default_factory=lambda: _env("MAX_UPLOAD_SIZE", 64 * GiB, int))
    upload_expiry_s: float = field(default_factory=lambda: _env("UPLOAD_EXPIRY_S", 7 * 86400.0, float))
    access_token_ttl_s: int = field(default_factory=lambda: _env("ACCESS_TTL_S", 15 * 60, int))
    refresh_token_ttl_s: int = field(default_factory=lambda: _env("REFRESH_TTL_S", 30 * 86400, int))
    cookie_secure: bool = field(default_factory=lambda: _env("COOKIE_SECURE", False, bool))
    web_dist: Path | None = field(default_factory=lambda: Path(p) if (p := _env("WEB_DIST", "")) else None)

    # Background workers
    enable_workers: bool = field(default_factory=lambda: _env("WORKERS", True, bool))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "meta.sqlite3"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def derived_dir(self) -> Path:
        return self.data_dir / "derived"

    @property
    def secret_path(self) -> Path:
        return self.data_dir / "jwt.secret"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.staging_dir / "uploads", self.staging_dir / "batches", self.derived_dir):
            d.mkdir(parents=True, exist_ok=True)
