"""Multi-index hashing (Norouzi et al.) for Hamming range search (TDD §9).

Split each 64-bit code into 4 disjoint 16-bit substrings, one hash table per
substring. By pigeonhole, if d(q, x) <= r then at least one substring of x is
within floor(r / 4) of q's substring. So: enumerate every 16-bit value within
floor(r/4) of each query substring, collect candidates, verify full distance.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations

M = 4
BITS = 64 // M
MASK = (1 << BITS) - 1


def _subs(h: int) -> list[int]:
    return [(h >> (BITS * i)) & MASK for i in range(M)]


def _neighbours(v: int, radius: int) -> list[int]:
    out = [v]
    for r in range(1, radius + 1):
        for bits in combinations(range(BITS), r):
            x = v
            for b in bits:
                x ^= 1 << b
            out.append(x)
    return out


class MultiIndexHash:
    def __init__(self) -> None:
        self.tables: list[dict[int, list[int]]] = [defaultdict(list) for _ in range(M)]
        self.codes: dict[int, int] = {}
        self.candidates_checked = 0

    def add(self, h: int, item_id: int) -> None:
        self.codes[item_id] = h
        for t, s in zip(self.tables, _subs(h)):
            t[s].append(item_id)

    def remove(self, h: int, item_id: int) -> None:
        self.codes.pop(item_id, None)
        for t, s in zip(self.tables, _subs(h)):
            lst = t.get(s)
            if lst and item_id in lst:
                lst.remove(item_id)

    def search(self, q: int, radius: int) -> list[tuple[int, int]]:
        sub_r = radius // M
        seen: set[int] = set()
        out = []
        for t, s in zip(self.tables, _subs(q)):
            for v in _neighbours(s, sub_r):
                for item in t.get(v, ()):
                    if item in seen:
                        continue
                    seen.add(item)
                    d = (self.codes[item] ^ q).bit_count()
                    if d <= radius:
                        out.append((item, d))
        self.candidates_checked += len(seen)
        return out
