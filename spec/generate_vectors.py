"""Generate spec/merkle_vectors.json from the server's Merkle implementation.

The iOS client (ios/CloudSyncKit) must produce identical node hashes; its tests
load this file. Regenerate with:  server/.venv/bin/python spec/generate_vectors.py
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from cloudstore.sync.merkle import MerkleTree  # noqa: E402


def h(i: int) -> str:
    return hashlib.sha256(f"item-{i}".encode()).hexdigest()


cases = []
for n in (0, 1, 2, 17, 300, 5000):
    hashes = [h(i) for i in range(n)]
    t = MerkleTree(hashes)
    probe = hashes[0][:3] if hashes else "abc"
    cases.append({
        "name": f"n={n}",
        "hashes": hashes,
        "root": t.root().hex(),
        "nodes": {p: t.node(p).hex() for p in ("", probe[:1], probe[:2], probe)},
        "children_of_root": [x.hex() for x in t.children("")],
        "bucket": {probe: t.bucket(probe)},
    })
out = Path(__file__).with_name("merkle_vectors.json")
out.write_text(json.dumps({"depth": 3, "empty": "00" * 32, "cases": cases}, indent=1))
print(f"wrote {out} ({out.stat().st_size} bytes)")
