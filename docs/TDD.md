# Technical Design Document — Self-Hosted Personal Cloud Storage

Status: v1.0 · Source: `brief-01-storage.md` (design brief 01) · Owner: Aryan Malhotra

This document expands the design brief into a build spec. Every decision the brief
delegated ("PRD agent must decide") is resolved here with a rationale, and every gap
found while reviewing the brief is closed with an explicit decision (see §15).
The code in this repository implements this document; section numbers are referenced
from module docstrings.

---

## 1. Scope

In scope (build): erasure-coded durable store with scrub/repair/rebuild, two-tier
ingest (media / non-media), content addressing, resumable tus upload, Merkle sync,
perceptual near-duplicate detection, CDC dedup with Bloom-fronted chunk index,
FastAPI backend, Google-Photos-style web app, iOS backup app, benchmark harnesses,
deployment for a mini PC on a Tailscale tailnet.

Designed, not built: ML tier (§13), Android/macOS/Windows clients.

## 2. Architecture

```
 iPhone (SwiftUI, PhotoKit, Core Data)          Browser (React + Vite)
   │  Merkle sync + tus upload                     │  REST + cookie JWT
   └──────────────── Tailscale (WireGuard mesh, MagicDNS, HTTPS via `tailscale serve`)
                                   │
                         FastAPI app (uvicorn, single process)
      ┌────────────┬─────────────┬─┴───────────┬──────────────┬─────────────┐
      auth/JWT     tus server    sync/Merkle   browse/search  admin/dashboard
                          │ staging (SSD)
                    ingest worker (durable job queue in SQLite)
          classify → EXIF → thumbnails/proxy → pHash → store → metadata commit
            │ media                                   │ non-media
            ▼                                         ▼
       ObjectStore  ◄──────────────────────────  BatchStore (CDC + Bloom + zstd)
   (Reed–Solomon k+m stripes, per-shard BLAKE2b checksums)
            │
   Disk set: /srv/cloud/disks/d1 … d6  (ext4/XFS, one shard per disk per stripe)
            ▲
   background workers: scrub · rebuild · disk health · batch sealer · trash purge
```

Metadata lives in SQLite (WAL mode) on the mini PC's system SSD. Derived data
(thumbnails, proxies) lives on the SSD in `derived/` and is regenerable. Only
original bytes live on the erasure-coded disks.

## 3. Hardware and storage profile

* **Default profile: RS(4,2)** — 6 disks, any 2 may fail, 66.7 % usable capacity.
  The owner has chosen 4+2 in hardware, so the physical deployment needs **six
  drive bays** (e.g. a 6-bay enclosure, or two enclosures / 4-bay + internal bays).
  A 4-bay enclosure cannot host 4+2 with one shard per failure domain.
* **Invariant:** every shard of a stripe lives on a distinct disk. The placement
  code refuses to put two shards of one stripe on one disk.
* **Bootstrap (fewer than k+m disks):** the store runs in *mirror* profile
  RS(1, m') with m' = min(disks−1, 2). RS with k = 1 and an all-ones parity row
  is exactly replication, so mirroring is the same code path, not a separate system.
* **Growth:** each stripe records its own (k, m). Adding disks does not rewrite
  data; `cloudstore restripe` migrates mirror stripes to the target profile in the
  background (read object → re-put with new profile → atomically swap → delete old).
* Per-disk filesystem ext4 or XFS, mounted by UUID with `nofail`. Each disk root
  carries `disk.json` (uuid, label) so a disk is identified by identity, not by
  mount path.

## 4. Erasure coding (centrepiece) — `server/cloudstore/erasure/`

### 4.1 Field
GF(2^8) with reducing polynomial x^8+x^4+x^3+x^2+1 (**0x11D**), generator
element **α = 0x02**. (The brief calls 0x11D a generator; it is the reducing
polynomial.) Addition = XOR. Multiplication via `EXP[LOG[a]+LOG[b]]`, with `EXP`
doubled to 512 entries to avoid a modulo. A full 256×256 multiplication table
(64 KiB) backs the vectorised paths; the SIMD path uses 16-entry nibble tables.

### 4.2 Code construction
Systematic code: encoding matrix `E = [I_k ; C]`, where C is an m×k **Cauchy**
matrix `C[i][j] = 1 / (x_i ⊕ y_j)` with `y_j = j` (0…k−1) and `x_i = k + i`.
The x and y sets are disjoint, so every square submatrix of C is non-singular,
and any k rows of E are invertible (MDS). Constraint k + m ≤ 256. For k = 1 the
parity rows are all ones (replication).

### 4.3 Encode / decode
* Encode: `parity_i = ⊕_j C[i][j]·data_j`, computed with a region kernel
  `mul_add(dst, src, c)` (dst ^= c·src over a byte buffer).
* Decode: choose any k surviving shard indices S (data shards preferred), build
  the k×k submatrix E_S, invert by Gauss–Jordan over GF(2^8), then
  `data = E_S^{-1} · survivors`. Inverses are cached per survivor set (LRU).
* Fast path: if all k data shards are healthy, reads are concatenation only
  (no field arithmetic), which is why healthy reads cost the same as mirroring.

### 4.4 Kernel backends (the benchmark story)
| backend | technique | where |
|---|---|---|
| `python` | byte loop with log/exp tables — reference, used by tests | `gf256.py` |
| `numpy` | 64 KiB product table + fancy indexing, XOR via ufunc | `backends.py` |
| `native` | C, split-nibble table lookup: ARM **NEON** `vqtbl1q_u8`, x86 **SSSE3/AVX2** `pshufb`, scalar fallback | `native/gf_simd.c` |

The native library is built with the system C compiler and loaded with `ctypes`
(no Python headers needed). The codec picks `native > numpy > python`, overridable
via `CLOUDSTORE_GF_BACKEND`.

### 4.5 Stripe layout
Objects are split into **per-object stripes** (no cross-object packing):
`shard_size = min(ceil(remaining / k) rounded up to 64 B, max_shard_size)`,
default `max_shard_size = 4 MiB` (16 MiB of data per 4+2 stripe).
* A 3 MB photo → one stripe of 6 shards ≈ 768 KB each.
* A 2 GB video → 128 stripes; a byte-range read touches only the stripes it overlaps,
  so video seeking during degraded mode decodes only what is needed. A 128 MiB LRU
  of decoded stripes serves the sequential range requests of video playback.
* Rationale (resolves the packing gap): phones produce objects ≥ 100 KB almost
  exclusively, so per-object stripes waste < 1 % to padding, while making delete
  trivial (drop the stripes — no compaction, no stripe-level GC). The only
  small-object producer, the non-media tier, already packs into 1 GiB batches.
  At 100 k photos, 4+2 is ≈100 k shard files per disk — comfortable for ext4/XFS.

### 4.6 Shard file format (self-describing)
256-byte header, then the payload:
```
offset size field                     offset size field
0      4    magic "CSHD"              64     4    stripe seq within object
4      1    version (1)               68     8    stripe data offset in object
5      1    shard index               76     8    stripe data length
6      1    k                         84     2    object key length
7      1    m                         86     ≤166 object key (e.g. media/<sha256>)
8      16   stripe id (UUID bytes)    252    4    CRC32 of header bytes 0..251
24     8    payload length (LE)
32     32   BLAKE2b-256 of payload
```
Path: `<disk>/shards/<id[0:2]>/<id[2:4]>/<stripe-id>.<index>.shd`.
BLAKE2b-256 (stdlib, faster than SHA-256 in software) is the payload checksum; it is
stored both in the header and in the `shards` table. Because shards are
self-describing, `cloudstore recover-index` rebuilds the objects/stripes/shards
tables by scanning the disks if the SQLite file is ever lost, and `--reingest`
re-derives file metadata (EXIF, thumbnails, hashes) from the recovered originals.

### 4.7 Write protocol (atomicity — resolves the "disk dies mid-write" concern)
1. Insert stripe row `state='pending'` (the journal entry), commit.
2. For each shard: write `<disk>/tmp/<name>.part`, `fsync`, `rename` into place,
   `fsync` the directory.
3. If ≥ `k + 1` shards landed (configurable via `CLOUDSTORE_MIN_WRITE_EXTRA`), insert shard rows
   (missing ones `state='missing'`, queued for repair) and flip the stripe to
   `committed` in one transaction. Otherwise delete what landed and fail the put.
4. On startup, `pending` stripes older than the process start are garbage
   (their object never committed): their shard files are deleted.

An object is only visible after all its stripes are committed and the object row
is committed — a crash never exposes a partially written file.

### 4.8 Read path
Read all data shards; verify checksums. Any missing/corrupt data shard → read
parity shards until k valid shards are held → decode. Every checksum failure is
recorded (`shards.state='corrupt'`) and queued for repair. Degraded-read latency
is measured by the benchmark (§12).

### 4.9 Scrub and repair
Background worker, rate-limited (default 50 MB/s so foreground reads win),
walks committed stripes ordered by `last_scrubbed_at`. For each stripe: read and
verify every shard; if 1…m are bad and ≥ k are good, reconstruct the bad shards
and rewrite them atomically (to the same disk if healthy, else to a spare disk
that holds no shard of that stripe). A stripe with < k good shards is flagged
`unrecoverable` and surfaced on the dashboard. Each run is recorded in
`scrub_runs` (bytes read, shards verified, corrupt found, repaired, unrecoverable).
Default schedule: full pass every 30 days, continuous background progress.

### 4.10 Disk failure detection and rebuild
* Health monitor (every 60 s): disk root reachable, `disk.json` identity matches,
  write/read probe, I/O error counter (from the store), optional SMART via
  `smartctl -j` (skipped when unavailable; USB enclosures must support SAT
  passthrough to expose SMART).
* States: `online → degraded (errors) → failed (unreachable)`, plus `rebuilding`,
  `retired`. Reads immediately stop targeting failed disks (degraded mode).
* Replacement: `cloudstore disk replace <old> <new-path>` registers the new disk,
  marks the old `retired`, and enqueues every stripe with a shard on the old disk.
  The rebuild worker reconstructs each lost shard onto the new disk, throttled,
  in its own thread; foreground reads/writes proceed (reads use the degraded path;
  new writes skip non-online disks). Progress (stripes done / total, bytes/s, ETA)
  is exposed on the dashboard.

## 5. Two-tier ingest and content addressing

* **Content address:** SHA-256 of the whole file. `files.sha256` is unique;
  uploading the same bytes from two devices yields one file (and each device's sync
  manifest references it). SHA-256 (not BLAKE2) because iOS CryptoKit hashes it
  natively and it is the public identity of a file.
* **Classification:** magic-byte sniffing, extension as tiebreaker.
  Media → erasure-coded object store, no compression: JPEG, PNG, GIF, WebP, HEIC/HEIF,
  AVIF, MP4/MOV/M4V (H.264/HEVC), 3GP. Non-media → batch store: everything else,
  including RAW (DNG, CR2, CR3, NEF, ARW, RAF, ORF, RW2), per the brief.
* **Pipeline** (`ingest/pipeline.py`, durable jobs in `ingest_jobs`): verify
  SHA-256 → classify → metadata (EXIF for images incl. HEIC; own MP4/MOV box parser
  for videos: `mvhd` creation time, `©xyz` GPS, dimensions) → thumbnail (256 px
  long edge, WebP) + proxy (1600 px, JPEG) → perceptual hash → durable store →
  metadata commit → add to device manifest → delete staging file. Every step is
  idempotent; a crash re-runs the job. Video thumbnails use `ffmpeg` when present
  (installed by the deploy script); otherwise a placeholder is generated.

## 6. Non-media tier — `server/cloudstore/dedup/`

* **CDC:** Rabin fingerprint over a 64-byte sliding window, irreducible polynomial
  of degree 53 (`0x3DA3358B4DC173`), precomputed out/mod tables (restic-style).
  Boundary when `fp & mask == 0` with min 16 KiB / avg 64 KiB / max 256 KiB.
  Implemented in C (native lib) with a pure-Python reference (identical output,
  tested). An inserted byte changes only the chunk containing it.
* **Chunk index:** `chunks(hash PK, size, batch_id, raw_offset, refcount)` in
  SQLite, fronted by a **Bloom filter** (m bits / k hashes derived from the
  chunk's own BLAKE2b digest via double hashing, sized for 10 M chunks at 1 %
  FPR ≈ 11.4 MiB) so the common miss path costs no index lookup. Rebuilt from
  the table on startup if the persisted filter is missing or stale.
* **Batches:** new chunks append to the single *open batch* (staging file on the
  SSD). **Seal policy (resolves open question): seal at ≥ 1 GiB raw or when the
  oldest unsealed byte is 6 hours old**, whichever first — so a trickle of
  documents is never left without erasure protection for long.
* **Batch format:** zstd **seekable frames** — chunks are laid out grouped by file
  type, compressed in ~4 MiB frames with a per-batch **trained zstd dictionary**
  (captures cross-file redundancy among small similar files), followed by a frame
  index and footer. Rationale: a single 1 GiB zstd frame would force decompressing
  up to 1 GiB to read one document; seekable frames keep random access while
  CDC removes long-range duplicates and the dictionary + 4 MiB windows capture
  local/cross-file redundancy. The sealed batch is stored as an erasure-coded object,
  and a chunk read is a byte-range read touching one frame.
* **GC / eviction policy:** deleting a non-media file decrements chunk refcounts.
  A sealed batch whose live ratio falls below 50 % is compacted: live chunks are
  re-appended to the open batch and the old batch object is deleted.

## 7. Merkle-tree sync — `server/cloudstore/sync/merkle.py`

* The server keeps, per device, a **device manifest**: the set of SHA-256 hashes
  that device currently has *and* the server stores. The client builds the same
  tree over its local library hashes.
* **Tree shape:** fixed-depth 16-ary trie on hash hex digits, **depth 3** (4096
  leaf buckets: ~25 items/bucket at 100 k). Leaf hash = SHA-256 of the sorted
  32-byte member hashes; empty leaf = 32 zero bytes; internal node = SHA-256 of its
  16 child hashes concatenated; root = level 0. Identical in Python and Swift;
  shared test vectors in `spec/merkle_vectors.json`.
* **Protocol** (all under `/api/sync/{device_id}`):
  1. `GET root` → `{root, count}`. Equal → done (one 32-byte comparison).
  2. `POST nodes {paths:[…]}` → child hashes for each path; client descends only
     into differing children, one round-trip per level (3 max).
  3. `POST buckets {paths:[…]}` → member hashes of differing leaves.
  4. Client computes `missing = local − server`, `extra = server − local`.
  5. `POST reconcile {add, remove}` → server links already-stored hashes
     (cross-device dedup: no upload needed), unlinks `remove` (a photo deleted on
     the phone leaves the device manifest, never the archive), and returns
     `need_upload` — the hashes the client must send via tus.
  Cost: O(changed buckets · (depth + bucket size)) instead of O(n) manifest.
* **Bulk fallback:** each changed leaf costs three levels of 16 child hashes, so when the
  difference is large relative to the library (first sync, or > 5 % changed) the client
  skips the tree walk and sends its hash list straight to `reconcile`. The benchmark
  shows the crossover (100 new items in a 1 k library is cheaper as a manifest).
* Per-device state on the phone (Core Data) records each asset's hash and
  `synced` status, so a confirmed asset is never re-hashed or re-scanned.

## 8. Resumable upload — tus 1.0.0 (`api/tus.py`)

Own implementation of core + `creation`, `termination`, `expiration`.
Metadata keys: `filename`, `sha256`, `mimetype`, `device_id`, `created_at`,
`local_id`. Uploads stage to `staging/uploads/<id>.part` on the SSD; incomplete
uploads expire after 7 days. On the final PATCH the server verifies the SHA-256,
enqueues the ingest job and replies `204`; ingest is asynchronous and the client
learns completion through the next sync (`reconcile`) or `GET /api/files/by-hash`.
Max upload size 64 GiB.

## 9. Perceptual near-duplicates — `server/cloudstore/phash/`

* Fingerprints: **pHash** (32×32 grayscale → 2-D DCT → top-left 8×8 excluding DC
  → median threshold → 64 bits) is primary; **dHash** (9×8 gradient) is stored too.
  EXIF orientation is applied first.
* Index: **multi-index hashing** (4 × 16-bit substrings; pigeonhole: d ≤ r ⇒ some
  substring within ⌊r/4⌋) is the production index. The **BK-tree** (the brief's first
  choice) is implemented and benchmarked alongside it, with a numpy vectorised-popcount
  linear scan as the honest baseline. Measured result (docs/benchmarks.md §5b): at
  r = 10 the BK-tree's triangle-inequality pruning still visits ~66 % of nodes (64-bit
  codes of unrelated photos sit at distance ≈ 32 ± 4, so the pruning window [d−r, d+r]
  covers most edges), while MIH answers in 0.4 ms at 100 k images vs 18 ms. The switch
  is one env var (`CLOUDSTORE_DUP_INDEX=bktree`).
* Grouping: new image → radius query (default r = 10 on pHash, tuned by the
  precision/recall benchmark) → union-find merge into `dup_groups`. Video frames
  are not fingerprinted in v1 (gap decision: burst shots are stills).
* UI: groups list with a suggested keeper (highest resolution, then sharpness via
  Laplacian variance, then earliest); "keep one" trashes the rest; space reclaimable
  is reported.

## 10. Browse, search, map, cache

* Timeline: `GET /api/timeline?cursor=` keyset pagination on
  `(capture_ts DESC, id DESC)`; month headers; thumbnails lazy-loaded; originals
  only on open. Albums, favourites, trash (30-day retention, restore, purge).
* Search: date range, camera make/model, near (lat, lon, radius km) using the
  geohash column for a prefix pre-filter + haversine refine, free-text on filename.
* Map: in-memory **point-region quadtree** with per-node aggregates
  (count, centroid, representative file). `GET /api/map/clusters?bbox&zoom` walks
  to the node size matching the zoom's cell size → O(visible clusters), not O(points).
  `geohash` (precision 9) stored per file for SQL filters.
* Thumbnail cache: **size-aware LRU** in memory (default 256 MiB byte budget) in
  front of the SSD `derived/` directory, plus prefetch of the next page window.
  Tuned by a scroll-trace simulation benchmark (§12).
* Storage dashboard: raw/usable/used capacity (usable = raw × k/(k+m)), per-disk
  status/usage/errors/SMART, scrub status and history, rebuild progress, stripe
  health counts.

## 11. Security

* Single user. Password set via `cloudstore set-password`, stored as scrypt
  (n=2^15, r=8, p=1) hash.
* `POST /api/auth/login` → access JWT (HS256, 15 min) + refresh token (random
  256-bit, 30 days, stored SHA-256-hashed, **rotated on every use**, reuse of a
  rotated token revokes the family). The web app receives both as HttpOnly,
  SameSite=Strict cookies; iOS uses the JSON body and `Authorization: Bearer`.
  Cookie-authenticated mutating requests must carry `X-Requested-With`.
* Network: the server binds to 127.0.0.1; `tailscale serve` exposes it on the
  tailnet with a MagicDNS HTTPS certificate (satisfies iOS ATS). Nothing is exposed
  to the public internet. Login attempts are rate-limited (5/min).

## 12. Benchmarks — `server/benchmarks/`

`python -m benchmarks.run_all` writes `docs/benchmarks.md`. All numbers are
labelled with the machine they ran on; virtual disks are directories.
1. RS encode throughput (MB/s) per backend × (k,m) ∈ {(2,1),(4,2),(6,2),(8,3)};
   vs XOR single parity (RAID-5) and vs mirroring (memcpy).
2. Reconstruct throughput for 1 and 2 lost shards; **extrapolated rebuild time for
   one dead 4 TB disk** (stated as an extrapolation from measured throughput).
3. Usable capacity vs mirroring at 2/4/6/8 disks and failures tolerated.
4. End-to-end object store: healthy vs degraded read latency (p50/p95),
   degraded-read latency while a rebuild runs.
5. Near-duplicates: precision/recall vs threshold on a labelled set (synthetic set
   generated by the harness; point it at a real folder-per-group set with
   `--labelled-dir`); query latency BK-tree vs MIH vs linear at 10 k and 100 k.
6. Merkle: bytes per sync check vs full manifest at 1 k/10 k/100 k items and
   0/1/10/100 changes.
7. CDC: dedup ratio on media vs non-media separately (the brief expects the media
   figure to be modest — the true number is reported), chunking throughput,
   Bloom false-positive rate and lookups avoided.
8. Thumbnail cache hit rate vs budget on simulated scroll traces.

## 13. ML roadmap (designed, not built)

* Semantic search: **on-server** (resolves open question). Rationale: the iPhone
  can only run background work opportunistically, embeddings must be searchable from
  the web app too, and the mini PC can run a quantised CLIP ViT-B/32 (int8 ONNX,
  ~90 MB) at a few images/s — enough for incremental indexing at ingest. Index:
  HNSW (M=16, ef_construction=200) in RAM (~100 k × 512 × 4 B ≈ 200 MB, or 50 MB at
  int8). Embeddings never leave the owner's hardware. Hook point: an extra ingest
  step writing to `embeddings(file_id, vector)`.
* Face clustering: detect (RetinaFace/SCRFD) → ArcFace embeddings → HDBSCAN for
  the initial clustering; incremental assignment of new faces to the nearest cluster
  centroid under a distance threshold, with a nightly re-consolidation that merges
  clusters whose centroids converge and re-runs HDBSCAN only on the unassigned pool.

## 14. Data model (SQLite; `server/cloudstore/db/schema.sql`)

`disks`, `objects`, `stripes`, `shards`, `files`, `file_chunks`, `chunks`,
`batches`, `devices`, `device_items` (sync manifest), `uploads` (tus),
`ingest_jobs`, `albums`, `album_items`, `dup_groups`, `thumbnails`, `scrub_runs`,
`rebuild_tasks`, `settings`, `refresh_tokens`. Favourites are a boolean column on
`files`. Metadata only — never file bytes.
**PostgreSQL migration path:** SQL is kept to the common subset (no SQLite-only
functions in queries except via `db.dialect` helpers); `INTEGER PRIMARY KEY`
maps to `BIGSERIAL`, `BLOB` to `BYTEA`, JSON text columns to `JSONB`; the
`Database` class is the single seam where a psycopg connection pool would replace
`sqlite3`.

### 14.1 Metadata durability
The SQLite file lives on a single SSD. Two layers protect it: (1) shard headers let
`recover-index` rebuild the object/stripe/shard tables from the disks alone; (2) a daily
**metadata snapshot** (SQLite online backup → zstd) is stored as an erasure-coded object
`meta/snapshot-<ts>` on the data disks (last 7 kept), so albums, favourites, trash and
sync manifests get the same any-two-disks durability as photos.
`cloudstore recover-index --restore-metadata --reingest` restores the newest snapshot,
re-indexes stripes written after it, and re-derives metadata for media newer than the
snapshot (their original filenames are not recoverable; content, EXIF and dates are).

## 15. Gap resolutions (decisions made while expanding the brief)

| # | Gap | Decision |
|---|---|---|
| 1 | Default k+m | 4+2 (owner decision), 6 bays; mirror profile below 6 disks; `restripe` to migrate |
| 2 | Small objects vs stripes | Per-object stripes with bounded shard size (§4.5) |
| 3 | Atomic multi-disk writes | Journaled pending→committed stripes, tmp+fsync+rename (§4.7) |
| 4 | Python speed for GF math | C SIMD kernel via ctypes + numpy fallback (§4.4) |
| 5 | Non-media durability | Sealed batches are erasure-coded objects (§6) |
| 6 | Batch threshold / eviction | 1 GiB or 6 h; compaction below 50 % live (§6) |
| 7 | Random access into 1 GiB zstd batch | Seekable 4 MiB frames + trained dictionary (§6) |
| 8 | Deleting content-addressed data | Trash with 30-day retention; chunk refcounts (§6, §10) |
| 9 | Merkle tree shape across devices | Per-device manifest + fixed-depth prefix trie (§7) |
| 10 | iCloud-optimised originals | App requests originals with network access allowed; hashing streams via `PHAssetResourceManager` (§16) |
| 11 | HEIC→JPEG transcoding changes hashes | Hash and upload the original resource bytes, never an export (§16) |
| 12 | Live Photos / edits | Upload the original photo resource and the paired video as separate files; edits not synced in v1 |
| 13 | Video near-duplicates | Out of scope v1 |
| 14 | Website framework | React 18 + Vite + TypeScript, Leaflet for the map; served by FastAPI as static files |
| 15 | Semantic search location | On-server (§13) |
| 16 | Web auth for `<img>` | HttpOnly cookies; bearer for iOS (§11) |
| 17 | Exposure | Bind localhost; `tailscale serve` HTTPS on the tailnet |
| 18 | Single-SSD metadata | Daily erasure-coded metadata snapshots + index recovery from shard headers (§14.1) |
| 19 | Near-duplicate index | MIH in production after measuring BK-tree pruning at r=10 (§9) |
| 20 | Server-side deletes vs phone re-upload | Tombstone rows (`purged_at`) keep the hash known, so `reconcile` never asks for it again |
| 21 | Web auth transport | HttpOnly SameSite=Strict cookies + `X-Requested-With` CSRF guard |

## 16. iOS client — `ios/`

* SwiftUI app + `CloudSyncKit` Swift package (platform-independent logic: Merkle
  tree, API client, tus client, sync planner — unit-tested with `swift test`).
* **UX: sync-on-open.** On foreground: incremental scan via
  `PHPhotoLibrary.fetchPersistentChanges(since:)` (iOS 16+; full scan on first run
  or expired token) → hash new/changed assets (streaming SHA-256 over
  `PHAssetResourceManager` with `isNetworkAccessAllowed = true`) → Merkle compare →
  reconcile → tus upload of `need_upload`.
* **Background:** `BGProcessingTask` (requires power, network) for bulk hashing and
  upload; `BGAppRefreshTask` for a quick sync check. Timing is best-effort; the UI
  says "last backed up …" honestly. Uploads use a background `URLSession` so an
  upload in flight continues after the app is suspended; tus `HEAD` resumes it.
* **Share extension:** uploads items shared from Photos or Files via the same
  tus client (config shared through an App Group).
* **Core Data:** `SyncItem(localIdentifier, modificationDate, resourceName,
  sha256, size, status[new|hashed|uploading|synced|failed], lastError, updatedAt)`
  plus `SyncMeta(changeToken, lastSyncAt)`.
* Settings: server URL (tailnet name), Wi-Fi only toggle, include videos toggle.

## 17. Build phases (as implemented)

| Phase | Content | Status |
|---|---|---|
| 1 | GF(2^8), Cauchy RS, python/numpy/C-SIMD backends, shard store, object store, scrub, rebuild, health, mirror/restripe, index recovery | done — tests + failure drills |
| 2 | DB schema, config, auth, tus, ingest pipeline (classify, EXIF, MP4 parser, thumbnails), files/albums/search/trash API | done |
| 3 | Merkle sync server + shared cross-language vectors | done |
| 4 | Non-media tier: Rabin CDC (C + Python), Bloom filter, chunk index, seekable dictionary-compressed batches, GC/compaction | done |
| 5 | pHash/dHash, BK-tree, MIH, duplicate groups; quadtree map clusters; size-aware LRU; dashboard | done |
| 6 | Benchmark harnesses + results (`docs/benchmarks.md`) | done |
| 7 | Web app (React + Vite) | done — verified in browser against a seeded 6-disk instance |
| 8 | iOS: CloudSyncKit (tested, incl. live e2e against the server) + SwiftUI app + share extension | done — app sources typechecked against iOS frameworks (Mac Catalyst SDK); on-device run requires Xcode + signing |
| 9 | Deployment (install script, systemd, Tailscale), metadata snapshots, CI | done |

**Minimum viable spine** (if time is short): phases 1, 2, 3, 5 (dedup part), 7, 8.
