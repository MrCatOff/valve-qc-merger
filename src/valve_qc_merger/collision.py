"""Detect where and how deeply the reference-hand mesh clips the weapon mesh.

Skeleton posing (retarget + finger IK) lands the fingers on the weapon's grip
contacts correctly, but the fuller reference-hand *mesh* can still push through
the weapon. Point-in-mesh tests against the *gun* are useless here: the gun is an
open, low-poly shell, so ray/winding "inside" tests read ~0 and nearest-vertex
distance reads large even mid-interpenetration.

Two robust measures are combined, each attributed to a grip *region* (thumb /
index / middle / ring / pinky / palm, per side):

* **Breadth** -- how many hand triangles clip the gun, by triangle intersection:
  a hand edge pierces a gun triangle, or a gun edge pierces the hand triangle.
  This needs no notion of inside/outside, so the open gun shell is fine.
* **Depth** -- how far the gun has sunk into the hand, measured the other way
  round: the *hand* is a closed solid, so a gun vertex counts as inside it by the
  generalized winding number (correct even in the concave gaps between fingers),
  and its distance to the nearest hand-surface triangle is the penetration depth.
  Measuring against the closed hand (not the gun) also dodges the large-triangle
  trap that makes nearest-vertex distance lie.

A uniform grid over the gun triangles keeps the breadth broad-phase cheap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from valve_qc_merger.models.smd import Smd
from valve_qc_merger.transform import Transform

_Vec = tuple[float, float, float]
_Tri = tuple[_Vec, _Vec, _Vec]
_CELL = 1.5  # broad-phase grid cell size, model units
_EPS = 1e-7
# A hand vertex more than this far behind a gun face is *behind the whole gun*
# (a posing issue), not poking through its material -- so it is not grip clipping.
_MAX_POKE = 1.5

REGIONS = ("thumb", "index", "middle", "ring", "pinky", "palm")


@dataclass(frozen=True, slots=True)
class RegionClip:
    """How the gun clips one grip region in a posed frame."""

    triangles: int  # hand triangles that pierce the gun (breadth)
    max_depth: float  # deepest interpenetration here, model units (see below)
    into_gun: float  # how far the hand pokes through the gun shell (e.g. a thumb tip)
    into_hand: float  # how far the gun sinks into the closed hand mesh (a finger gap)


def _sub(a: _Vec, b: _Vec) -> _Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: _Vec, b: _Vec) -> _Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a: _Vec, b: _Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit_normal(tri: _Tri, toward: _Vec) -> _Vec:
    """Unit normal of ``tri``, flipped to point toward ``toward`` (e.g. the hand)."""
    n = _cross(_sub(tri[1], tri[0]), _sub(tri[2], tri[0]))
    length = math.sqrt(_dot(n, n))
    if length < _EPS:
        return (0.0, 0.0, 0.0)
    n = (n[0] / length, n[1] / length, n[2] / length)
    return n if _dot(n, _sub(toward, tri[0])) >= 0.0 else (-n[0], -n[1], -n[2])


def _segment_pierces_triangle(p: _Vec, q: _Vec, a: _Vec, b: _Vec, c: _Vec) -> bool:
    """True when open segment ``p``->``q`` passes through triangle ``a,b,c``."""
    direction = _sub(q, p)
    edge1 = _sub(b, a)
    edge2 = _sub(c, a)
    h = _cross(direction, edge2)
    det = _dot(edge1, h)
    if -_EPS < det < _EPS:
        return False  # segment parallel to the triangle
    inv = 1.0 / det
    s = _sub(p, a)
    u = _dot(s, h) * inv
    if u < 0.0 or u > 1.0:
        return False
    qv = _cross(s, edge1)
    v = _dot(direction, qv) * inv
    if v < 0.0 or u + v > 1.0:
        return False
    t = _dot(edge2, qv) * inv
    return _EPS < t < 1.0 - _EPS  # strictly between the segment's endpoints


def _triangles_intersect(t1: _Tri, t2: _Tri) -> bool:
    a1, b1, c1 = t1
    a2, b2, c2 = t2
    return (
        _segment_pierces_triangle(a1, b1, a2, b2, c2)
        or _segment_pierces_triangle(b1, c1, a2, b2, c2)
        or _segment_pierces_triangle(c1, a1, a2, b2, c2)
        or _segment_pierces_triangle(a2, b2, a1, b1, c1)
        or _segment_pierces_triangle(b2, c2, a1, b1, c1)
        or _segment_pierces_triangle(c2, a2, a1, b1, c1)
    )


def _solid_angle(a: _Vec, b: _Vec, c: _Vec) -> float:
    """Signed solid angle subtended by triangle ``a,b,c`` at the origin."""
    la = math.sqrt(_dot(a, a))
    lb = math.sqrt(_dot(b, b))
    lc = math.sqrt(_dot(c, c))
    num = _dot(a, _cross(b, c))
    den = la * lb * lc + _dot(a, b) * lc + _dot(b, c) * la + _dot(c, a) * lb
    return 2.0 * math.atan2(num, den)


def _inside_mesh(p: _Vec, tris: list[_Tri]) -> bool:
    """Generalized winding number: is ``p`` inside the closed mesh ``tris``?"""
    total = 0.0
    for a, b, c in tris:
        total += _solid_angle(_sub(a, p), _sub(b, p), _sub(c, p))
    return abs(total) > 2.0 * math.pi  # |winding| > 0.5 turns


def _point_triangle_dist2(p: _Vec, a: _Vec, b: _Vec, c: _Vec) -> float:
    """Squared distance from ``p`` to the closest point on triangle ``a,b,c``."""
    ab = _sub(b, a)
    ac = _sub(c, a)
    ap = _sub(p, a)
    d1 = _dot(ab, ap)
    d2 = _dot(ac, ap)
    if d1 <= 0.0 and d2 <= 0.0:
        return _dot(ap, ap)
    bp = _sub(p, b)
    d3 = _dot(ab, bp)
    d4 = _dot(ac, bp)
    if d3 >= 0.0 and d4 <= d3:
        return _dot(bp, bp)
    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        t = d1 / (d1 - d3)
        proj = (a[0] + t * ab[0], a[1] + t * ab[1], a[2] + t * ab[2])
        d = _sub(p, proj)
        return _dot(d, d)
    cp = _sub(p, c)
    d5 = _dot(ab, cp)
    d6 = _dot(ac, cp)
    if d6 >= 0.0 and d5 <= d6:
        return _dot(cp, cp)
    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        t = d2 / (d2 - d6)
        proj = (a[0] + t * ac[0], a[1] + t * ac[1], a[2] + t * ac[2])
        d = _sub(p, proj)
        return _dot(d, d)
    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        t = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        bc = _sub(c, b)
        proj = (b[0] + t * bc[0], b[1] + t * bc[1], b[2] + t * bc[2])
        d = _sub(p, proj)
        return _dot(d, d)
    denom = 1.0 / (va + vb + vc)
    v = vb * denom
    w = vc * denom
    proj = (
        a[0] + ab[0] * v + ac[0] * w,
        a[1] + ab[1] * v + ac[1] * w,
        a[2] + ab[2] * v + ac[2] * w,
    )
    d = _sub(p, proj)
    return _dot(d, d)


def _region_of(name: str) -> str:
    side = "L" if "_L_" in name else ("R" if "_R_" in name else "?")
    for index, region in enumerate(("thumb", "index", "middle", "ring", "pinky")):
        if f"Finger{index}" in name:
            return f"{side}_{region}"
    return f"{side}_palm"


def _bbox(tri: _Tri) -> tuple[_Vec, _Vec]:
    xs = (tri[0][0], tri[1][0], tri[2][0])
    ys = (tri[0][1], tri[1][1], tri[2][1])
    zs = (tri[0][2], tri[1][2], tri[2][2])
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _cells(lo: _Vec, hi: _Vec) -> list[tuple[int, int, int]]:
    ranges = [range(int(lo[i] // _CELL), int(hi[i] // _CELL) + 1) for i in range(3)]
    return [(x, y, z) for x in ranges[0] for y in ranges[1] for z in ranges[2]]


def _world_triangles(smd: Smd, world: dict[int, Transform]) -> list[tuple[_Tri, str]]:
    """Pose ``smd``'s triangles into world space, tagged by their grip region."""
    names = {node.index: node.name for node in smd.nodes}
    out: list[tuple[_Tri, str]] = []
    for triangle in smd.triangles:
        pts: list[_Vec] = []
        bones: list[int] = []
        for vertex in triangle.vertices:
            transform = world.get(vertex.bone)
            if transform is None:
                break
            p = transform.transform_point(vertex.position)
            pts.append((p.x, p.y, p.z))
            bones.append(vertex.bone)
        if len(pts) != 3:
            continue
        region = _region_of(names.get(max(set(bones), key=bones.count), ""))
        out.append(((pts[0], pts[1], pts[2]), region))
    return out


def grip_collisions(
    hand: Smd, gun: Smd, world: dict[int, Transform], measure_into_hand: bool = True
) -> dict[str, RegionClip]:
    """Per-region breadth and depth of the hand mesh clipping the gun, one frame.

    ``hand`` and ``gun`` must share the merged skeleton, and ``world`` its bone
    world transforms for the frame (``kinematics.world_transforms``). Regions are
    ``"<side>_<part>"`` (e.g. ``"R_thumb"``); only clipping regions appear.

    ``measure_into_hand`` runs the winding-number pass for the gun-into-hand
    depth; set it false to skip that cost when only the fast ``thru-gun`` /
    triangle breadth is needed (e.g. inside a solver loop).
    """
    hand_tris = _world_triangles(hand, world)
    hand_plain = [tri for tri, _ in hand_tris]
    gun_tris = [tri for tri, _ in _world_triangles(gun, world)]
    n = float(len(hand_plain) * 3) or 1.0
    centroid = (
        sum(t[i][0] for t in hand_plain for i in range(3)) / n,
        sum(t[i][1] for t in hand_plain for i in range(3)) / n,
        sum(t[i][2] for t in hand_plain for i in range(3)) / n,
    )

    # --- breadth + into-gun depth: hand triangles that pierce the gun ---
    # The gun shell is open, so how far the hand pokes *through* it is measured
    # locally, against the plane of each pierced gun face (oriented toward the
    # hand), so a large gun triangle can't inflate the number.
    grid: dict[tuple[int, int, int], list[int]] = {}
    for i, tri in enumerate(gun_tris):
        lo, hi = _bbox(tri)
        for cell in _cells(lo, hi):
            grid.setdefault(cell, []).append(i)
    triangles: dict[str, int] = {}
    into_gun: dict[str, float] = {}
    for hand_tri, region in hand_tris:
        a, b, c = hand_tri
        lo, hi = _bbox(hand_tri)
        candidates: set[int] = set()
        for cell in _cells(lo, hi):
            candidates.update(grid.get(cell, ()))
        hit = False
        for i in candidates:
            g = gun_tris[i]
            normal = _unit_normal(g, centroid)
            for p, q in ((a, b), (b, c), (c, a)):
                if _segment_pierces_triangle(p, q, *g):
                    hit = True
                    # Depth is the far (into-gun) endpoint's distance behind the
                    # pierced face -- bounded by the short hand edge, so a finger
                    # merely *behind* the whole gun can't inflate it.
                    for endpoint in (p, q):
                        pen = -_dot(_sub(endpoint, g[0]), normal)
                        if into_gun.get(region, 0.0) < pen <= _MAX_POKE:
                            into_gun[region] = pen
            if not hit:
                ga, gb, gc = g
                if (
                    _segment_pierces_triangle(ga, gb, a, b, c)
                    or _segment_pierces_triangle(gb, gc, a, b, c)
                    or _segment_pierces_triangle(gc, ga, a, b, c)
                ):
                    hit = True
        if hit:
            triangles[region] = triangles.get(region, 0) + 1

    # --- into-hand depth: gun vertices sunk inside the (closed) hand mesh ---
    into_hand: dict[str, float] = {}
    hlo = [min(t[i][k] for t in hand_plain for i in range(3)) for k in range(3)]
    hhi = [max(t[i][k] for t in hand_plain for i in range(3)) for k in range(3)]
    seen: set[tuple[int, int, int]] = set()
    for tri in gun_tris if measure_into_hand else ():
        for v in tri:
            if not all(hlo[k] <= v[k] <= hhi[k] for k in range(3)):
                continue
            key = (round(v[0] * 100), round(v[1] * 100), round(v[2] * 100))
            if key in seen:
                continue
            seen.add(key)
            if not _inside_mesh(v, hand_plain):
                continue
            best = min(
                (_point_triangle_dist2(v, *tri_r), region) for tri_r, region in hand_tris
            )
            region = best[1]
            into_hand[region] = max(into_hand.get(region, 0.0), math.sqrt(best[0]))

    result: dict[str, RegionClip] = {}
    for region in set(triangles) | set(into_hand):
        d_gun = into_gun.get(region, 0.0)
        d_hand = into_hand.get(region, 0.0)
        result[region] = RegionClip(
            triangles=triangles.get(region, 0),
            max_depth=max(d_gun, d_hand),
            into_gun=d_gun,
            into_hand=d_hand,
        )
    return result


__all__ = ["grip_collisions", "RegionClip", "REGIONS"]
