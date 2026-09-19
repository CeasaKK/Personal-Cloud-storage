"""BK-tree over 64-bit hashes under Hamming distance (TDD §9).

Each node stores a hash; its children are keyed by their distance to it. For a
query q with radius r, the triangle inequality means only children whose edge
distance d satisfies |d(q, node) - r| <= d <= d(q, node) + r can contain
matches, which prunes most of the tree for small r.
"""

from __future__ import annotations

from typing import Iterator


class _Node:
    __slots__ = ("hash", "ids", "children")

    def __init__(self, h: int, item_id: int) -> None:
        self.hash = h
        self.ids = [item_id]
        self.children: dict[int, _Node] = {}


class BKTree:
    def __init__(self) -> None:
        self.root: _Node | None = None
        self.size = 0
        self.nodes_visited = 0  # instrumentation for benchmarks

    def add(self, h: int, item_id: int) -> None:
        self.size += 1
        if self.root is None:
            self.root = _Node(h, item_id)
            return
        node = self.root
        while True:
            d = (node.hash ^ h).bit_count()
            if d == 0:
                node.ids.append(item_id)
                return
            child = node.children.get(d)
            if child is None:
                node.children[d] = _Node(h, item_id)
                return
            node = child

    def remove(self, h: int, item_id: int) -> bool:
        """Lazy delete: drop the id; empty nodes stay as routing nodes."""
        node = self.root
        while node is not None:
            d = (node.hash ^ h).bit_count()
            if d == 0:
                if item_id in node.ids:
                    node.ids.remove(item_id)
                    self.size -= 1
                    return True
                return False
            node = node.children.get(d)
        return False

    def search(self, q: int, radius: int) -> list[tuple[int, int]]:
        """All (item_id, distance) with Hamming distance <= radius."""
        out: list[tuple[int, int]] = []
        if self.root is None:
            return out
        stack = [self.root]
        visited = 0
        while stack:
            node = stack.pop()
            visited += 1
            d = (node.hash ^ q).bit_count()
            if d <= radius:
                out.extend((i, d) for i in node.ids)
            lo, hi = d - radius, d + radius
            for edge, child in node.children.items():
                if lo <= edge <= hi:
                    stack.append(child)
        self.nodes_visited += visited
        return out

    def __iter__(self) -> Iterator[tuple[int, int]]:
        stack = [self.root] if self.root else []
        while stack:
            n = stack.pop()
            for i in n.ids:
                yield n.hash, i
            stack.extend(n.children.values())
