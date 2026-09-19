-- Metadata schema (TDD §14). Metadata only: pointers, hashes, attributes.
-- Kept to the SQL subset shared with PostgreSQL; see TDD §14 for the mapping.

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- durable store
CREATE TABLE IF NOT EXISTS disks (
    id             INTEGER PRIMARY KEY,
    uuid           TEXT UNIQUE NOT NULL,
    label          TEXT NOT NULL,
    path           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'online',  -- online|degraded|failed|rebuilding|retired
    error_count    INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    smart_json     TEXT,
    capacity_bytes INTEGER,
    free_bytes     INTEGER,
    added_at       REAL NOT NULL,
    last_seen_at   REAL
);

CREATE TABLE IF NOT EXISTS objects (
    id           INTEGER PRIMARY KEY,
    key          TEXT UNIQUE NOT NULL,
    size         INTEGER NOT NULL,
    stripe_count INTEGER NOT NULL,
    state        TEXT NOT NULL DEFAULT 'pending',   -- pending|committed
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS stripes (
    id               TEXT PRIMARY KEY,               -- uuid hex
    object_id        INTEGER REFERENCES objects(id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL,
    k                INTEGER NOT NULL,
    m                INTEGER NOT NULL,
    shard_size       INTEGER NOT NULL,
    data_offset      INTEGER NOT NULL,
    data_len         INTEGER NOT NULL,
    state            TEXT NOT NULL,                  -- pending|committed|unrecoverable|superseded
    created_at       REAL NOT NULL,
    last_scrubbed_at REAL
);
CREATE INDEX IF NOT EXISTS stripes_object ON stripes(object_id, seq);
CREATE INDEX IF NOT EXISTS stripes_scrub ON stripes(state, last_scrubbed_at);

CREATE TABLE IF NOT EXISTS shards (
    stripe_id TEXT NOT NULL REFERENCES stripes(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    disk_id   INTEGER REFERENCES disks(id),
    checksum  BLOB,
    is_parity INTEGER NOT NULL,
    state     TEXT NOT NULL DEFAULT 'ok',            -- ok|missing|corrupt
    PRIMARY KEY (stripe_id, idx)
);
CREATE INDEX IF NOT EXISTS shards_disk ON shards(disk_id);
CREATE INDEX IF NOT EXISTS shards_bad ON shards(state) WHERE state != 'ok';

CREATE TABLE IF NOT EXISTS scrub_runs (
    id              INTEGER PRIMARY KEY,
    started_at      REAL NOT NULL,
    finished_at     REAL,
    status          TEXT NOT NULL,                   -- running|done|aborted
    stripes_checked INTEGER NOT NULL DEFAULT 0,
    shards_verified INTEGER NOT NULL DEFAULT 0,
    bytes_read      INTEGER NOT NULL DEFAULT 0,
    corrupt_found   INTEGER NOT NULL DEFAULT 0,
    missing_found   INTEGER NOT NULL DEFAULT 0,
    repaired        INTEGER NOT NULL DEFAULT 0,
    unrecoverable   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rebuild_tasks (
    id             INTEGER PRIMARY KEY,
    source_disk_id INTEGER NOT NULL REFERENCES disks(id),
    target_disk_id INTEGER REFERENCES disks(id),
    status         TEXT NOT NULL,                    -- queued|running|done|failed
    total_shards   INTEGER NOT NULL DEFAULT 0,
    done_shards    INTEGER NOT NULL DEFAULT 0,
    failed_shards  INTEGER NOT NULL DEFAULT 0,
    bytes_written  INTEGER NOT NULL DEFAULT 0,
    started_at     REAL,
    finished_at    REAL,
    error          TEXT
);

-- ---------------------------------------------------------------- files
CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY,
    sha256       TEXT UNIQUE NOT NULL,
    size         INTEGER NOT NULL,
    mime         TEXT NOT NULL,
    kind         TEXT NOT NULL,                      -- image|video|other
    tier         TEXT NOT NULL,                      -- media|nonmedia
    filename     TEXT NOT NULL,
    capture_ts   REAL NOT NULL,
    upload_ts    REAL NOT NULL,
    width        INTEGER,
    height       INTEGER,
    duration     REAL,
    camera_make  TEXT,
    camera_model TEXT,
    lat          REAL,
    lon          REAL,
    geohash      TEXT,
    exif_json    TEXT,
    object_id    INTEGER REFERENCES objects(id),     -- media tier
    favorite     INTEGER NOT NULL DEFAULT 0,
    trashed_at   REAL,
    phash        INTEGER,
    dhash        INTEGER,
    sharpness    REAL,
    dup_group    INTEGER,
    dup_reviewed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS files_timeline ON files(trashed_at, capture_ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS files_geohash ON files(geohash) WHERE geohash IS NOT NULL;
CREATE INDEX IF NOT EXISTS files_camera ON files(camera_model);
CREATE INDEX IF NOT EXISTS files_dup ON files(dup_group) WHERE dup_group IS NOT NULL;
CREATE INDEX IF NOT EXISTS files_tier ON files(tier, trashed_at);

-- ---------------------------------------------------------------- non-media tier
CREATE TABLE IF NOT EXISTS batches (
    id               INTEGER PRIMARY KEY,
    state            TEXT NOT NULL,                  -- open|sealing|sealed|deleted
    raw_bytes        INTEGER NOT NULL DEFAULT 0,
    live_bytes       INTEGER NOT NULL DEFAULT 0,
    compressed_bytes INTEGER,
    frame_count      INTEGER,
    object_id        INTEGER REFERENCES objects(id),
    created_at       REAL NOT NULL,
    first_data_at    REAL,
    sealed_at        REAL
);

CREATE TABLE IF NOT EXISTS chunks (
    hash       BLOB PRIMARY KEY,                     -- BLAKE2b-256
    size       INTEGER NOT NULL,
    batch_id   INTEGER NOT NULL REFERENCES batches(id),
    raw_offset INTEGER NOT NULL,                     -- offset in the batch's raw layout
    refcount   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chunks_batch ON chunks(batch_id);

CREATE TABLE IF NOT EXISTS file_chunks (
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    chunk_hash BLOB NOT NULL,
    PRIMARY KEY (file_id, seq)
);

-- ---------------------------------------------------------------- devices / sync
CREATE TABLE IF NOT EXISTS devices (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    platform     TEXT NOT NULL,
    created_at   REAL NOT NULL,
    last_sync_at REAL,
    last_seen_at REAL
);

CREATE TABLE IF NOT EXISTS device_items (
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    sha256    TEXT NOT NULL,
    added_at  REAL NOT NULL,
    PRIMARY KEY (device_id, sha256)
);

CREATE TABLE IF NOT EXISTS uploads (
    id           TEXT PRIMARY KEY,
    length       INTEGER NOT NULL,
    offset       INTEGER NOT NULL DEFAULT 0,
    metadata     TEXT NOT NULL,
    device_id    TEXT,
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS ingest_jobs (
    id           INTEGER PRIMARY KEY,
    staging_path TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    metadata     TEXT NOT NULL,
    state        TEXT NOT NULL,                      -- queued|running|done|failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    file_id      INTEGER,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ingest_state ON ingest_jobs(state, id);

-- ---------------------------------------------------------------- organisation
CREATE TABLE IF NOT EXISTS albums (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    cover_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS album_items (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    added_at REAL NOT NULL,
    PRIMARY KEY (album_id, file_id)
);

CREATE TABLE IF NOT EXISTS thumbnails (
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    variant    TEXT NOT NULL,                        -- thumb|proxy
    path       TEXT NOT NULL,
    bytes      INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (file_id, variant)
);

-- ---------------------------------------------------------------- auth
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          INTEGER PRIMARY KEY,
    token_hash  TEXT UNIQUE NOT NULL,
    family      TEXT NOT NULL,
    client      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    revoked_at  REAL,
    replaced_by INTEGER
);
