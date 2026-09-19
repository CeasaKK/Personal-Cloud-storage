import os

import pytest

from cloudstore.config import Config
from cloudstore.db.database import Database
from cloudstore.storage.disks import DiskManager
from cloudstore.storage.objectstore import ObjectStore


def make_config(tmp_path, **overrides) -> Config:
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.fsync = False
    cfg.max_shard_size = 64 * 1024
    cfg.scrub_rate_bytes = 0
    cfg.rebuild_rate_bytes = 0
    cfg.enable_workers = False
    for k, v in overrides.items():
        setattr(cfg, k, v)
    cfg.ensure_dirs()
    return cfg


class Env:
    def __init__(self, tmp_path, n_disks=6, **overrides):
        self.config = make_config(tmp_path, **overrides)
        self.db = Database(self.config.db_path)
        self.disks = DiskManager(self.db)
        self.disk_list = [self.disks.add(tmp_path / f"disk{i}", f"d{i}") for i in range(n_disks)]
        self.store = ObjectStore(self.db, self.disks, self.config)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def rand():
    return os.urandom
