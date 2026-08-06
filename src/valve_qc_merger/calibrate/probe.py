"""Point-to-surface penetration probe for weapon shells (dependency-free).

The retarget stage-2 calibration signal: how deep a posed hand vertex sits behind
the nearest weapon face. GoldSource weapon shells are open, low-poly meshes, so an
enclosure or nearest-vertex test misses the intrusion (see the project notes on
"gun mesh open-shell intrusion"); a signed point-to-triangle-surface distance,
oriented by the mesh's own vertex normals, does not.

Pure Python (no numpy) so the baking path can reuse it without the ``[calibrate]``
GUI extra installed.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from valve_qc_merger.models.smd import Smd, Triangle
from valve_qc_merger.parsers.smd import parse_smd_file

Vec3 = tuple[float, float, float]


def _nrm(a: Vec3) -> Vec3:
    m = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
    return (a[0] / m, a[1] / m, a[2] / m) if m else a


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _sub3(p: Vec3, q: Vec3) -> Vec3:
    return (p[0] - q[0], p[1] - q[1], p[2] - q[2])


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _closest_on_tri(p: Vec3, a: Vec3, b: Vec3, c: Vec3) -> Vec3:
    """Closest point on triangle ``abc`` to ``p`` (Ericson, Real-Time Collision
    Detection §5.1.5 — barycentric region test, no matrix solve)."""
    ab, ac, ap = _sub3(b, a), _sub3(c, a), _sub3(p, a)
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        return a
    bp = _sub3(p, b)
    d3, d4 = _dot(ab, bp), _dot(ac, bp)
    if d3 >= 0 and d4 <= d3:
        return b
    vc = d1 * d4 - d3 * d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        v = d1 / (d1 - d3)
        return (a[0] + ab[0] * v, a[1] + ab[1] * v, a[2] + ab[2] * v)
    cp = _sub3(p, c)
    d5, d6 = _dot(ab, cp), _dot(ac, cp)
    if d6 >= 0 and d5 <= d6:
        return c
    vb = d5 * d2 - d1 * d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        w = d2 / (d2 - d6)
        return (a[0] + ac[0] * w, a[1] + ac[1] * w, a[2] + ac[2] * w)
    va = d3 * d6 - d5 * d4
    if va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        return (b[0] + (c[0] - b[0]) * w, b[1] + (c[1] - b[1]) * w, b[2] + (c[2] - b[2]) * w)
    denom = 1.0 / (va + vb + vc)
    v, w = vb * denom, vc * denom
    return (a[0] + ab[0] * v + ac[0] * w,
            a[1] + ab[1] * v + ac[1] * w,
            a[2] + ab[2] * v + ac[2] * w)


class WeaponMesh:
    """Weapon triangles + a point-to-surface min-distance query (open shells ok)."""

    def __init__(self, source: str | Smd | Sequence[Triangle],
                 material_prefix: str | None = None) -> None:
        # source: an SMD file path (str) OR an already-parsed Smd OR a list of Triangle
        if isinstance(source, str):
            triangles: Sequence[Triangle] = parse_smd_file(source).triangles
        elif isinstance(source, Smd):
            triangles = source.triangles
        else:
            triangles = source
        self.tris: list[tuple[Vec3, Vec3, Vec3]] = []
        self._nout: list[Vec3] = []  # per-tri OUTWARD unit normal (via vertex normals)
        for t in triangles:
            if material_prefix and not t.material.lower().startswith(material_prefix):
                continue
            a, b, c = ((v.position.x, v.position.y, v.position.z) for v in t.vertices)
            self.tris.append((a, b, c))
            fn = _cross(_sub3(b, a), _sub3(c, a))
            # orient outward: agree with the mesh's own (outward-pointing) vertex normals
            vn = (sum(v.normal[0] for v in t.vertices),
                  sum(v.normal[1] for v in t.vertices),
                  sum(v.normal[2] for v in t.vertices))
            if _dot(fn, vn) < 0:
                fn = (-fn[0], -fn[1], -fn[2])
            self._nout.append(_nrm(fn))
        self._bb: list[tuple[float, float, float, float, float, float]] = [
            (min(x[0] for x in tr), min(x[1] for x in tr), min(x[2] for x in tr),
             max(x[0] for x in tr), max(x[1] for x in tr), max(x[2] for x in tr))
            for tr in self.tris
        ]

    def min_distance(self, p: Vec3, cutoff: float = 1e9) -> float:
        return self.probe(p, cutoff)[0]

    def probe(self, p: Vec3, cutoff: float = 1e9) -> tuple[float, float, Vec3]:
        """(surface_distance, penetration, outward_normal) for point p. penetration > 0
        means p sits BEHIND the nearest weapon face (inside the shell) by that many units
        — i.e. the hand is entering the weapon; <= 0 means it is outside (a gap). The
        outward_normal is the nearest face's, i.e. the direction to push the weapon to
        change this point's penetration 1:1. Works even for an open low-poly shell."""
        best = cutoff
        bcp: Vec3 | None = None
        bn: Vec3 | None = None
        px, py, pz = p
        for tr, bb, n in zip(self.tris, self._bb, self._nout, strict=True):
            dx = 0.0 if bb[0] <= px <= bb[3] else min(abs(px - bb[0]), abs(px - bb[3]))
            dy = 0.0 if bb[1] <= py <= bb[4] else min(abs(py - bb[1]), abs(py - bb[4]))
            dz = 0.0 if bb[2] <= pz <= bb[5] else min(abs(pz - bb[2]), abs(pz - bb[5]))
            if dx * dx + dy * dy + dz * dz >= best * best:
                continue
            cp = _closest_on_tri(p, tr[0], tr[1], tr[2])
            d = math.dist(p, cp)
            if d < best:
                best, bcp, bn = d, cp, n
        if bcp is None or bn is None:
            return best, 0.0, (0.0, 0.0, 0.0)
        pen = -_dot(_sub3(p, bcp), bn)  # behind the face -> positive penetration
        return best, pen, bn


__all__ = ["WeaponMesh"]
