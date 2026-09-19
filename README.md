# Cloudstore: self-hosted personal cloud storage

A self-hosted replacement for Google Photos that runs on your own hardware. The iPhone
backs up its camera roll, and you browse, search and manage everything from a
Google-Photos-style web app.

The storage layer is written from scratch rather than delegated to ZFS or a NAS.
Photos are stored with **Reed–Solomon erasure coding** across six disks: 67% of raw
capacity is usable, versus 50% for mirroring, and any **two** disks can fail at once.
A background scrub finds and repairs silent bit rot.

```
iPhone ──(Merkle sync + resumable tus upload)──┐        ┌── Browser (React)
                                                ▼        ▼
                          Tailscale mesh (private, HTTPS, no port forwarding)
                                                │
                                     FastAPI server (mini PC)
                 ┌──────────────┬───────────────┼────────────────┬──────────────┐
            ingest (EXIF,   near-duplicates   Merkle sync    map quadtree   dashboard
            thumbnails)     (pHash + MIH)
                 │ media                         │ documents / RAW
                 ▼                               ▼
     Reed–Solomon 4+2 object store  ◄──  CDC + Bloom filter + zstd batches
                 │
       6 disks · per-shard BLAKE2b · scrub · rebuild · daily metadata snapshots
```

## What's inside

| Area | Implementation | Where |
|---|---|---|
| **Erasure coding** | GF(2⁸) arithmetic, systematic Cauchy RS(k,m), Gauss–Jordan decode, and a C split-nibble SIMD kernel (NEON / AVX2 / SSSE3), **11.5× faster than scalar C**, ~15 GB/s encode on an M2 | `server/cloudstore/erasure/` |
| **Durable store** | Per-object stripes, one shard per disk, journaled atomic writes, degraded reads, self-describing 256-byte shard headers | `server/cloudstore/storage/objectstore.py` |
| **Integrity** | BLAKE2b per shard, rate-limited scrub with repair, disk health (probe + SMART), background rebuild onto a replacement disk | `storage/scrub.py`, `rebuild.py`, `disks.py` |
| **Disaster recovery** | The index can be rebuilt from shard headers, and metadata snapshots are themselves stored erasure-coded, so a lost SSD is recoverable | `storage/recovery.py`, `snapshots.py` |
| **Near-duplicates** | pHash/dHash, **multi-index hashing** (chosen over the BK-tree after benchmarking), union-find grouping, keeper suggestion | `server/cloudstore/phash/` |
| **Sync** | Fixed-depth Merkle trie, bit-identical in Python and Swift. An up-to-date check is ~100 bytes vs 6.8 MB for a 100k-item manifest | `sync/merkle.py`, `ios/CloudSyncKit` |
| **Non-media tier** | Rabin CDC (C + Python reference), a Bloom-fronted chunk index, and 1 GiB batches compressed as seekable zstd frames with a trained dictionary | `server/cloudstore/dedup/` |
| **Upload** | Own tus 1.0 server with SHA-256 verification on completion | `api/tus.py` |
| **Web app** | Timeline (justified grid), viewer, map clusters, search, albums, duplicates, files, trash, storage dashboard | `web/` |
| **iOS app** | SwiftUI, incremental PhotoKit scan, original-resource hashing, Core Data state, BGTaskScheduler, share extension | `ios/` |

The design and every decision behind it are in **[docs/TDD.md](docs/TDD.md)**. Measured
results are in **[docs/benchmarks.md](docs/benchmarks.md)**, reported honestly: for
example, CDC dedup on photos is 1.00×.

## Quick start (local demo, about 2 minutes)

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m cloudstore.erasure.native          # build the SIMD kernel
cd ../web && npm ci && npm run build && cd ../server
CLOUDSTORE_DATA_DIR=./demo/data .venv/bin/python scripts/demo_seed.py   # 6 virtual disks + 200 photos
CLOUDSTORE_DATA_DIR=./demo/data .venv/bin/cloudstore serve
```

Open http://127.0.0.1:8000 and sign in with `demo-password-123`. To see the durability
in action, delete `server/demo/disks/d2`: every photo still opens. Then run
`cloudstore disk replace 2 ./demo/disks/d7` and watch the rebuild on the Storage page.

## Deploying for real

See **[docs/DEPLOY.md](docs/DEPLOY.md)**: the mini PC plus 6 disks, `deploy/install.sh`,
the systemd unit, and Tailscale. The iOS app is covered in **[ios/README.md](ios/README.md)**.

## Tests

```bash
cd server && .venv/bin/python -m pytest -q            # 81 tests: codec, SIMD kernels, store, failure drills, formats, API, disaster recovery
cd ios/CloudSyncKit && swift test                      # Merkle vectors shared with the server, planner, tus
cd server && .venv/bin/python -m benchmarks.run_all    # regenerates docs/benchmarks.md
```

## Repository layout

```
server/      FastAPI backend, storage engine, workers, CLI, tests, benchmarks
web/         React + Vite management site (served by the backend in production)
ios/         CloudSyncKit Swift package + SwiftUI app + share extension (XcodeGen)
spec/        cross-language test vectors (Merkle)
deploy/      install script, systemd unit, env template
docs/        TDD, benchmarks, deployment guide
```
