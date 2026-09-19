"""Point-region quadtree with per-node aggregates for map clustering (TDD §10).

Points live in Web-Mercator unit space (x, y in [0, 1)), matching the map's
tile pyramid: at zoom z a 256 px tile spans 1/2^z, so clustering at depth z+2
yields ~64 px clusters. Every node keeps (count, sum_x, sum_y, representative),
so a viewport query returns one cluster per non-empty cell at the target depth
without touching individual points: O(visible cells), not O(points).
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

LEAF_CAPACITY = 16
MAX_DEPTH = 24


def to_mercator(lat: float, lon: float) -> tuple[float, float]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    x = (lon + 180.0) / 360.0
    s = math.sin(math.radians(lat))
    y = 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)
    return min(max(x, 0.0), 1 - 1e-12), min(max(y, 0.0), 1 - 1e-12)


def from_mercator(x: float, y: float) -> tuple[float, float]:
    lon = x * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y))))
    return lat, lon


@dataclass
class _Node:
    x0: float
    y0: float
    size: float
    depth: int
    count: int = 0
    sx: float = 0.0
    sy: float = 0.0
    rep: tuple[float, int] | None = None  # (sort key e.g. capture_ts, file id)
    points: list[tuple[float, float, int, float]] | None = field(default_factory=list)
    children: list["_Node"] | None = None

    def child_for(self, x: float, y: float) -> "_Node":
        half = self.size / 2
        i = (1 if x >= self.x0 + half else 0) + (2 if y >= self.y0 + half else 0)
        return self.children[i]  # type: ignore[index]


class QuadTree:
    def __init__(self) -> None:
        self.root = _Node(0.0, 0.0, 1.0, 0)
        self._lock = threading.Lock()
        self.size = 0

    def insert(self, lat: float, lon: float, item_id: int, key: float = 0.0) -> None:
        x, y = to_mercator(lat, lon)
        with self._lock:
            self._insert(self.root, x, y, item_id, key)
            self.size += 1

    def _insert(self, node: _Node, x: float, y: float, item_id: int, key: float) -> None:
        while True:
            node.count += 1
            node.sx += x
            node.sy += y
            if node.rep is None or key >= node.rep[0]:
                node.rep = (key, item_id)
            if node.children is None:
                node.points.append((x, y, item_id, key))  # type: ignore[union-attr]
                if len(node.points) > LEAF_CAPACITY and node.depth < MAX_DEPTH:  # type: ignore[arg-type]
                    self._split(node)
                return
            node = node.child_for(x, y)

    def _split(self, node: _Node) -> None:
        half = node.size / 2
        node.children = [
            _Node(node.x0, node.y0, half, node.depth + 1),
            _Node(node.x0 + half, node.y0, half, node.depth + 1),
            _Node(node.x0, node.y0 + half, half, node.depth + 1),
            _Node(node.x0 + half, node.y0 + half, half, node.depth + 1),
        ]
        pts, node.points = node.points, None
        for x, y, i, k in pts:  # type: ignore[union-attr]
            c = node.child_for(x, y)
            self._insert(c, x, y, i, k)

    def remove(self, lat: float, lon: float, item_id: int) -> bool:
        x, y = to_mercator(lat, lon)
        with self._lock:
            path = []
            node = self.root
            while node.children is not None:
                path.append(node)
                node = node.child_for(x, y)
            before = len(node.points)  # type: ignore[arg-type]
            node.points = [p for p in node.points if p[2] != item_id]  # type: ignore[union-attr]
            if len(node.points) == before:
                return False
            for n in path + [node]:
                n.count -= 1
                n.sx -= x
                n.sy -= y
            for n in reversed(path + [node]):  # bottom-up so parents see fresh child reps
                if n.rep and n.rep[1] == item_id:
                    n.rep = self._find_rep(n)
            self.size -= 1
            return True

    def _find_rep(self, node: _Node) -> tuple[float, int] | None:
        if node.count == 0:
            return None
        if node.children is None:
            best = max(node.points, key=lambda p: p[3])  # type: ignore[arg-type]
            return (best[3], best[2])
        reps = [c.rep for c in node.children if c.count and c.rep]
        return max(reps) if reps else None

    def clusters(self, west: float, south: float, east: float, north: float, zoom: int) -> list[dict]:
        """Clusters for a viewport. Handles antimeridian-crossing boxes."""
        if west > east:
            return self.clusters(west, south, 180.0, north, zoom) + self.clusters(-180.0, south, east, north, zoom)
        x0, y1 = to_mercator(south, west)
        x1, y0 = to_mercator(north, east)
        target = min(max(zoom, 0) + 2, MAX_DEPTH)
        cells: dict[tuple[int, int], list] = {}
        out: list[dict] = []
        with self._lock:
            stack = [self.root]
            while stack:
                n = stack.pop()
                if n.count == 0 or n.x0 > x1 or n.x0 + n.size < x0 or n.y0 > y1 or n.y0 + n.size < y0:
                    continue
                if n.depth >= target:
                    out.append(self._emit(n.count, n.sx, n.sy, n.rep))
                elif n.children is not None:
                    stack.extend(n.children)
                else:
                    # shallow leaf: bucket its points by target-depth cell
                    scale = 1 << target
                    for x, y, i, k in n.points:  # type: ignore[union-attr]
                        if not (x0 <= x <= x1 and y0 <= y <= y1):
                            continue
                        cell = cells.setdefault((int(x * scale), int(y * scale)), [0, 0.0, 0.0, None])
                        cell[0] += 1
                        cell[1] += x
                        cell[2] += y
                        if cell[3] is None or k >= cell[3][0]:
                            cell[3] = (k, i)
        for cnt, sx, sy, rep in cells.values():
            out.append(self._emit(cnt, sx, sy, rep))
        return out

    @staticmethod
    def _emit(count: int, sx: float, sy: float, rep) -> dict:
        lat, lon = from_mercator(sx / count, sy / count)
        return {"lat": lat, "lon": lon, "count": count, "file_id": rep[1] if rep else None}
