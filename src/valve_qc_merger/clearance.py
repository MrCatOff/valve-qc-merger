"""Auto-compute how far to slide a weapon so it does not sit inside the hands.

When the reference hands grip a weapon, the gun mesh can end up slightly inside
the hand mesh. The hands cannot be reshaped, so instead the weapon is slid along
a grip-preserving direction (the user picks the direction they can see; this
module computes the distance).

A gun vertex counts as *inside* the hand when it lies on the interior side of
its nearest hand-surface vertex (``(gun - hand) . hand_normal < 0``). The slide
distance is the smallest translation along the chosen direction that brings the
count of inside vertices down to a small fraction of the original -- enough to
clear the obvious intrusion while tolerating the slight texture overlap the user
accepts. A uniform grid over the hand vertices keeps the nearest-vertex query
fast.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import Smd

_CELL = 1.0  # grid cell size in model units


def _normalize(v: Vector3) -> Vector3:
    length = math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)
    if length == 0.0:
        raise ValueError("clearance direction must be non-zero")
    return Vector3(v.x / length, v.y / length, v.z / length)


def _dedupe(points: Iterable[tuple[Vector3, Vector3]]) -> list[tuple[Vector3, Vector3]]:
    seen: set[tuple[int, int, int]] = set()
    out: list[tuple[Vector3, Vector3]] = []
    for position, normal in points:
        key = (round(position.x * 4), round(position.y * 4), round(position.z * 4))
        if key not in seen:
            seen.add(key)
            out.append((position, normal))
    return out


class _HandGrid:
    """Uniform spatial grid over hand vertices for nearest-vertex queries."""

    def __init__(self, vertices: list[tuple[Vector3, Vector3]]) -> None:
        self._cells: dict[tuple[int, int, int], list[tuple[Vector3, Vector3]]] = {}
        for position, normal in vertices:
            self._cells.setdefault(self._cell(position), []).append((position, normal))

    @staticmethod
    def _cell(p: Vector3) -> tuple[int, int, int]:
        return (
            int(math.floor(p.x / _CELL)),
            int(math.floor(p.y / _CELL)),
            int(math.floor(p.z / _CELL)),
        )

    def nearest(self, p: Vector3) -> tuple[Vector3, Vector3] | None:
        cx, cy, cz = self._cell(p)
        best: tuple[Vector3, Vector3] | None = None
        best_d2 = float("inf")
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for hp, hn in self._cells.get((cx + dx, cy + dy, cz + dz), ()):  # noqa: E501
                        d2 = (p.x - hp.x) ** 2 + (p.y - hp.y) ** 2 + (p.z - hp.z) ** 2
                        if d2 < best_d2:
                            best_d2 = d2
                            best = (hp, hn)
        return best


def _inside_count(gun: list[Vector3], grid: _HandGrid, shift: Vector3) -> int:
    count = 0
    for g in gun:
        p = Vector3(g.x + shift.x, g.y + shift.y, g.z + shift.z)
        nearest = grid.nearest(p)
        if nearest is None:
            continue
        hp, hn = nearest
        if (p.x - hp.x) * hn.x + (p.y - hp.y) * hn.y + (p.z - hp.z) * hn.z < 0.0:
            count += 1
    return count


def weapon_clearance_offset(
    hand: Smd,
    weapon: Smd,
    direction: Vector3,
    *,
    keep_fraction: float = 0.15,
    margin: float = 0.15,
    max_distance: float = 8.0,
) -> tuple[Vector3, int, int]:
    """Return ``(offset, inside_before, inside_after)`` for sliding the weapon.

    ``offset`` slides ``weapon`` along ``direction`` far enough to reduce the
    number of gun vertices inside ``hand`` to ``keep_fraction`` of the original
    (plus ``margin``), capped at ``max_distance``.
    """
    unit = _normalize(direction)
    hand_vertices = _dedupe(
        (v.position, v.normal) for triangle in hand.triangles for v in triangle.vertices
    )
    gun_vertices = [
        p for p, _ in _dedupe(
            (v.position, v.position) for triangle in weapon.triangles for v in triangle.vertices
        )
    ]
    grid = _HandGrid(hand_vertices)

    baseline = _inside_count(gun_vertices, grid, Vector3(0.0, 0.0, 0.0))
    if baseline == 0:
        return Vector3(0.0, 0.0, 0.0), 0, 0
    target = max(1, int(round(baseline * keep_fraction)))

    def at(distance: float) -> int:
        return _inside_count(
            gun_vertices, grid, Vector3(unit.x * distance, unit.y * distance, unit.z * distance)
        )

    if at(max_distance) > target:
        # Cannot reach the target along this direction; use the best we can.
        distance = max_distance
    else:
        low, high = 0.0, max_distance
        for _ in range(24):
            mid = (low + high) / 2.0
            if at(mid) <= target:
                high = mid
            else:
                low = mid
        distance = high
    distance += margin
    offset = Vector3(unit.x * distance, unit.y * distance, unit.z * distance)
    return offset, baseline, at(distance)


__all__ = ["weapon_clearance_offset"]
