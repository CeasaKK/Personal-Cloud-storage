"""Operator CLI: `cloudstore <command>`.

    cloudstore serve [--host 127.0.0.1 --port 8000]
    cloudstore set-password
    cloudstore disk add PATH [--label L] | disk list | disk replace OLD_ID NEW_PATH | disk check
    cloudstore scrub [--full]
    cloudstore rebuild           # run queued rebuild tasks in the foreground
    cloudstore restripe          # migrate mirror stripes to the k+m profile
    cloudstore recover-index [--restore-metadata] [--reingest]
    cloudstore snapshot          # metadata snapshot to the disks now (also runs daily)
    cloudstore seal              # seal open non-media batches now
    cloudstore status
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
import tempfile
from pathlib import Path

from .config import Config


def _svc():
    from .services import Services

    return Services(Config())


def _fmt_bytes(n: float | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def cmd_serve(args) -> None:
    import uvicorn

    uvicorn.run("cloudstore.app:main_app", factory=True, host=args.host, port=args.port,
                proxy_headers=True, log_level="info")


def cmd_set_password(args) -> None:
    from .auth import Auth, AuthError

    s = _svc()
    pw = getpass.getpass("New password (min 12 chars): ")
    if pw != getpass.getpass("Repeat: "):
        sys.exit("passwords do not match")
    try:
        Auth(s.db, s.config).set_password(pw)
    except AuthError as e:
        sys.exit(str(e))
    print("password set; existing sessions revoked")


def cmd_disk(args) -> None:
    s = _svc()
    if args.action == "add":
        d = s.disks.add(args.path, args.label)
        k, m = s.store.profile()
        print(f"added disk {d.id} ({d.label}) at {d.path}; write profile now k={k} m={m}")
        if k != s.config.ec_k:
            need = s.config.ec_k + s.config.ec_m - len(s.disks.list())
            print(f"mirror mode until {need} more disk(s) are added; then run `cloudstore restripe`")
    elif args.action == "list":
        for d in s.disks.list():
            n = s.db.scalar("SELECT COUNT(*) FROM shards WHERE disk_id = ?", (d.id,))
            print(f"{d.id:>3} {d.label:<12} {d.status:<10} errors={d.error_count:<4} shards={n:<8} "
                  f"free={_fmt_bytes(d.free_bytes)}/{_fmt_bytes(d.capacity_bytes)}  {d.path}")
    elif args.action == "check":
        for r in s.disks.check_all(with_smart=True):
            print(r)
    elif args.action == "replace":
        tid = s.rebuild.replace_disk(int(args.old_id), args.new_path, args.label)
        print(f"rebuild task {tid} queued; the server's rebuild worker will run it "
              f"(or run `cloudstore rebuild` to run it now)")


def cmd_scrub(args) -> None:
    s = _svc()
    s.store.recover_pending()
    print(json.dumps(s.scrubber.run(full=args.full), indent=2))


def cmd_rebuild(args) -> None:
    s = _svc()
    for tid in s.rebuild.pending_tasks():
        print(json.dumps(s.rebuild.run_task(tid), indent=2, default=str))


def cmd_restripe(args) -> None:
    s = _svc()
    print(f"restriped {s.store.restripe_all()} objects to profile {s.store.profile()}")


def cmd_recover_index(args) -> None:
    """Rebuild metadata from the disks (TDD §4.6, §14.1). After losing the SSD:
    re-register the disks (`cloudstore disk add PATH` for each), then run
    `cloudstore recover-index --restore-metadata --reingest`."""
    from .services import Services
    from .storage.recovery import recover_index
    from .storage.snapshots import latest_snapshot, restore_snapshot

    s = _svc()
    print(json.dumps(recover_index(s.db, s.disks), indent=2))
    if args.restore_metadata:
        key = latest_snapshot(s.db)
        if key is None:
            print("no metadata snapshot found on the disks; keeping the recovered object index only")
        else:
            paths = [d.path for d in s.disks.list() if d.status != "retired"]
            n = restore_snapshot(s.store, key, s.config.db_path)
            s.db.close()
            print(f"restored {key} ({n} bytes)")
            s = Services(Config())
            for p in paths:  # refresh mount paths; registers disks added after the snapshot
                s.disks.add(p)
            s.disks.check_all()
            print("objects written after the snapshot:", json.dumps(recover_index(s.db, s.disks)))
    if args.reingest:
        # media objects without a files row: re-derive metadata from the originals
        rows = s.db.all("SELECT key FROM objects WHERE key LIKE 'media/%' AND state = 'committed' "
                        "AND substr(key, 7) NOT IN (SELECT sha256 FROM files)")
        for r in rows:
            sha = r["key"][6:]
            with tempfile.NamedTemporaryFile(dir=s.config.staging_dir / "uploads", delete=False) as tmp:
                for part in s.store.iter_range(r["key"], 0, s.store.stat(r["key"])["size"]):
                    tmp.write(part)
            s.ingest.enqueue(Path(tmp.name), sha, {"filename": f"{sha[:12]}"})
        n = s.ingest.run_pending(limit=len(rows) + 1)
        print(f"re-ingested {n} media files")
    print("next: run `cloudstore scrub --full` to verify every shard")


def cmd_snapshot(args) -> None:
    from .storage.snapshots import take_snapshot

    s = _svc()
    print(take_snapshot(s.db, s.store))


def cmd_seal(args) -> None:
    s = _svc()
    for r in s.db.all("SELECT id FROM batches WHERE state IN ('open','sealing') AND raw_bytes > 0"):
        print(json.dumps(s.batches.seal(r["id"])))


def cmd_status(args) -> None:
    s = _svc()
    print(json.dumps({"capacity": s.store.capacity(), "stripes": s.store.stripe_health(),
                      "nonmedia": s.batches.stats()}, indent=2, default=str))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="cloudstore", description="self-hosted personal cloud storage")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(fn=cmd_serve)
    sub.add_parser("set-password").set_defaults(fn=cmd_set_password)
    dp = sub.add_parser("disk")
    dsub = dp.add_subparsers(dest="action", required=True)
    a = dsub.add_parser("add")
    a.add_argument("path")
    a.add_argument("--label")
    dsub.add_parser("list")
    dsub.add_parser("check")
    r = dsub.add_parser("replace")
    r.add_argument("old_id")
    r.add_argument("new_path")
    r.add_argument("--label")
    dp.set_defaults(fn=cmd_disk)
    sc = sub.add_parser("scrub")
    sc.add_argument("--full", action="store_true")
    sc.set_defaults(fn=cmd_scrub)
    sub.add_parser("rebuild").set_defaults(fn=cmd_rebuild)
    sub.add_parser("restripe").set_defaults(fn=cmd_restripe)
    ri = sub.add_parser("recover-index")
    ri.add_argument("--reingest", action="store_true")
    ri.add_argument("--restore-metadata", action="store_true")
    ri.set_defaults(fn=cmd_recover_index)
    sub.add_parser("seal").set_defaults(fn=cmd_seal)
    sub.add_parser("snapshot").set_defaults(fn=cmd_snapshot)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
