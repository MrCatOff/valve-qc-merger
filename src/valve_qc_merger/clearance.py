"""Auto-compute how far to slide a weapon so it fits the reference hands.

The reference hands cannot be reshaped, so hand/weapon interpenetration is
resolved by moving the *weapon*. Two facts make this well-posed:

* The palm *cups* the grip, so a radial push makes the overlap worse. The
  weapon must slide along a grip-preserving direction -- by default, away from
  the hand along the barrel (from the hand centroid toward the gun body).
* A hand gripping a weapon always overlaps it a little; that is a normal grip,
  not a defect. The original weapon hands establish how much overlap is
  acceptable. The reference hands, being a different shape, overlap more, so the
  weapon is slid only until the reference overlap drops to the original hands'
  level plus a small margin -- not to zero.

A gun vertex counts as *inside* a hand when it lies on the interior side of its
nearest hand-surface vertex (``(gun - hand) . hand_normal < 0``). A uniform grid
over the hand vertices keeps the nearest-vertex query fast.
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


def _hand_vertices(hand: Smd) -> list[tuple[Vector3, Vector3]]:
    return _dedupe((v.position, v.normal) for triangle in hand.triangles for v in triangle.vertices)


def _gun_vertices(weapon: Smd) -> list[Vector3]:
    return [p for p, _ in _dedupe(
        (v.position, v.position) for triangle in weapon.triangles for v in triangle.vertices
    )]


def _centroid(points: Iterable[Vector3]) -> Vector3:
    total = Vector3(0.0, 0.0, 0.0)
    count = 0
    for p in points:
        total = Vector3(total.x + p.x, total.y + p.y, total.z + p.z)
        count += 1
    if count == 0:
        return Vector3(0.0, 0.0, 0.0)
    return Vector3(total.x / count, total.y / count, total.z / count)


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
                    for hp, hn in self._cells.get((cx + dx, cy + dy, cz + dz), ()):
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


def gun_vertices_inside_hand(hand: Smd, weapon: Smd) -> int:
    """Number of weapon vertices that sit inside the hand mesh (no shift)."""
    grid = _HandGrid(_hand_vertices(hand))
    return _inside_count(_gun_vertices(weapon), grid, Vector3(0.0, 0.0, 0.0))


def away_direction(hand: Smd, weapon: Smd) -> Vector3:
    """Default slide direction: from the hand centroid toward the gun body."""
    hand_centroid = _centroid(p for p, _ in _hand_vertices(hand))
    gun_centroid = _centroid(_gun_vertices(weapon))
    delta = Vector3(
        gun_centroid.x - hand_centroid.x,
        gun_centroid.y - hand_centroid.y,
        gun_centroid.z - hand_centroid.z,
    )
    return _normalize(delta)


def _contact_centroid(
    smd: Smd, keep: object, grid: _HandGrid, radius: float
) -> Vector3 | None:
    near: list[Vector3] = []
    for triangle in smd.triangles:
        for v in triangle.vertices:
            if not keep(v.bone):  # type: ignore[operator]
                continue
            nearest = grid.nearest(v.position)
            if nearest is None:
                continue
            hp, _ = nearest
            if (v.position.x - hp.x) ** 2 + (v.position.y - hp.y) ** 2 + (
                v.position.z - hp.z
            ) ** 2 < radius * radius:
                near.append(v.position)
    return _centroid(near) if near else None


def palm_seat_offset(
    our_hand: Smd,
    original_hand: Smd,
    weapon: Smd,
    our_palm_bones: set[str],
    original_finger_bones: set[int],
    *,
    contact_radius: float = 1.5,
) -> Vector3:
    """Offset that seats the grip in the palm like the original hands did.

    A weapon rig's own hands grip it correctly. The part of the *original* palm
    that touches the gun marks where the grip belongs; the part of *our* palm
    that touches the gun marks where it currently sits. Their difference is the
    translation that moves the gun so our palm holds the grip where the original
    palm did. Returns zero when neither palm makes contact (nothing to seat).
    """
    grid = _HandGrid([(p, p) for p in _gun_vertices(weapon)])
    our_indices = {node.index for node in our_hand.nodes if node.name in our_palm_bones}
    ours = _contact_centroid(our_hand, lambda b: b in our_indices, grid, contact_radius)
    original = _contact_centroid(
        original_hand, lambda b: b not in original_finger_bones, grid, contact_radius
    )
    if ours is None or original is None:
        return Vector3(0.0, 0.0, 0.0)
    return Vector3(ours.x - original.x, ours.y - original.y, ours.z - original.z)


def weapon_clearance_offset(
    hand: Smd,
    weapon: Smd,
    direction: Vector3,
    *,
    target_inside: int = 0,
    margin_verts: int = 3,
    margin_distance: float = 0.1,
    max_distance: float = 8.0,
) -> tuple[Vector3, int, int]:
    """Return ``(offset, inside_before, inside_after)`` for sliding the weapon.

    Slides ``weapon`` along ``direction`` far enough to reduce the count of gun
    vertices inside ``hand`` to ``target_inside + margin_verts`` (the original
    hands' overlap plus a slight margin), capped at ``max_distance``.
    """
    unit = _normalize(direction)
    grid = _HandGrid(_hand_vertices(hand))
    gun = _gun_vertices(weapon)

    baseline = _inside_count(gun, grid, Vector3(0.0, 0.0, 0.0))
    target = target_inside + margin_verts
    if baseline <= target:
        return Vector3(0.0, 0.0, 0.0), baseline, baseline

    def at(distance: float) -> int:
        shift = Vector3(unit.x * distance, unit.y * distance, unit.z * distance)
        return _inside_count(gun, grid, shift)

    if at(max_distance) > target:
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
    distance += margin_distance
    offset = Vector3(unit.x * distance, unit.y * distance, unit.z * distance)
    return offset, baseline, at(distance)


__all__ = [
    "away_direction",
    "gun_vertices_inside_hand",
    "palm_seat_offset",
    "weapon_clearance_offset",
]
