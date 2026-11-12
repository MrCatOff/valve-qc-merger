"""What is at a point of a map: empty, solid, water … for a hull.

The compiler builds the world four times: hull 0 for points (``nodes`` and
``leafs``), and hulls 1-3 with the walls pushed out by a box (``clipnodes``)
— 1 a standing player (±16 × ±16 × ±36), 2 a large monster, 3 a crouching
player (±16 × ±16 × ±18). "Does a standing player fit with its origin
here?" is then one walk down hull 1 for the point itself. Points go in
batches (numpy), every point a walk of its own.

:func:`trace_hull` moves a box from one point to another as the engine does
(``SV_RecursiveHullCheck``: the first hit, a 1/32 unit off the wall);
:func:`segments_clear` asks the same of many segments at once by sampling
them (a player hull is 32 units wide, so a step under 16 misses no wall).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.geometry import geometry

EMPTY, SOLID, WATER, SLIME, LAVA, SKY = -1, -2, -3, -4, -5, -6
CONTENTS = {EMPTY: "empty", SOLID: "solid", WATER: "water", SLIME: "slime", LAVA: "lava",
            SKY: "sky"}
STAND, LARGE, CROUCH = 1, 2, 3
HULL_HALF = {1: (16.0, 16.0, 36.0), 2: (32.0, 32.0, 32.0), 3: (16.0, 16.0, 18.0)}


def point_contents(bsp: B.Bsp, points: np.ndarray, hull: int = 0, model: int = 0
                   ) -> np.ndarray:
    """The contents (``EMPTY``, ``SOLID`` …) at each of ``points`` (N, 3)
    for ``hull`` of brush model ``model`` (0: the world)."""
    geo = geometry(bsp)
    pts = np.atleast_2d(np.asarray(points, np.float64))
    out = np.full(len(pts), SOLID, np.int64)
    if not len(geo.models):
        return out
    num = np.full(len(pts), int(geo.models[model]["headnode"][hull]), np.int64)
    normals = geo.planes["normal"].astype(np.float64)
    dists = geo.planes["dist"].astype(np.float64)
    if hull == 0:
        planes = geo.nodes["plane"].astype(np.int64)
        children = geo.nodes["children"].astype(np.int64)
    else:
        planes = geo.clipnodes["plane"].astype(np.int64)
        children = geo.clipnodes["children"].astype(np.int64)
    active = num >= 0
    limit = len(planes) + 1
    while active.any() and limit:
        limit -= 1
        idx = num[active]
        plane = planes[idx]
        d = (pts[active] * normals[plane]).sum(axis=1) - dists[plane]
        num[active] = np.where(d >= 0, children[idx, 0], children[idx, 1])
        active = num >= 0
    if hull == 0:
        leaf = -1 - num
        ok = (leaf >= 0) & (leaf < len(geo.leafs))
        out[ok] = geo.leafs["contents"][leaf[ok]]
    else:
        out = num
    return out


DIST_EPSILON = 0.03125


@dataclass
class Trace:
    fraction: float = 1.0  # how far along the move the box got
    end: np.ndarray = field(default_factory=lambda: np.zeros(3))
    normal: np.ndarray = field(default_factory=lambda: np.zeros(3))  # of the wall hit
    start_solid: bool = False
    all_solid: bool = True


class _Hull:
    def __init__(self, bsp: B.Bsp, hull: int, model: int) -> None:
        geo = geometry(bsp)
        self.normals = geo.planes["normal"].astype(np.float64)
        self.dists = geo.planes["dist"].astype(np.float64)
        source = geo.nodes if hull == 0 else geo.clipnodes
        self.planes = source["plane"].astype(np.int64)
        self.children = source["children"].astype(np.int64)
        self.leaf_contents = geo.leafs["contents"].astype(np.int64) if hull == 0 else None
        self.head = int(geo.models[model]["headnode"][hull]) if len(geo.models) else SOLID

    def contents_of(self, num: int) -> int:
        """A child reference -> its contents (hull 0: through the leaf)."""
        if self.leaf_contents is None:
            return num
        leaf = -1 - num
        return int(self.leaf_contents[leaf]) if 0 <= leaf < len(self.leaf_contents) else SOLID

    def contents(self, num: int, point: np.ndarray) -> int:
        while num >= 0:
            plane = self.planes[num]
            d = float(self.normals[plane] @ point) - self.dists[plane]
            num = int(self.children[num, 0 if d >= 0 else 1])
        return self.contents_of(num)

    def check(self, num: int, p1f: float, p2f: float, p1: np.ndarray, p2: np.ndarray,
              trace: Trace) -> bool:
        if num < 0:
            value = self.contents_of(num)
            if value != SOLID:
                trace.all_solid = False
            else:
                trace.start_solid = True
            return True
        plane = self.planes[num]
        normal, dist = self.normals[plane], self.dists[plane]
        t1 = float(normal @ p1) - dist
        t2 = float(normal @ p2) - dist
        if t1 >= 0 and t2 >= 0:
            return self.check(int(self.children[num, 0]), p1f, p2f, p1, p2, trace)
        if t1 < 0 and t2 < 0:
            return self.check(int(self.children[num, 1]), p1f, p2f, p1, p2, trace)
        frac = (t1 + DIST_EPSILON) / (t1 - t2) if t1 < 0 else (t1 - DIST_EPSILON) / (t1 - t2)
        frac = min(max(frac, 0.0), 1.0)
        midf = p1f + (p2f - p1f) * frac
        mid = p1 + frac * (p2 - p1)
        side = 1 if t1 < 0 else 0
        if not self.check(int(self.children[num, side]), p1f, midf, p1, mid, trace):
            return False
        if self.contents(int(self.children[num, side ^ 1]), mid) != SOLID:
            return self.check(int(self.children[num, side ^ 1]), midf, p2f, mid, p2, trace)
        if trace.all_solid:
            return False
        trace.normal = normal.copy() if side == 0 else -normal
        while self.contents(self.head, mid) == SOLID:  # back off until outside
            frac -= 0.1
            if frac < 0:
                trace.fraction, trace.end = midf, mid
                return False
            midf = p1f + (p2f - p1f) * frac
            mid = p1 + frac * (p2 - p1)
        trace.fraction, trace.end = midf, mid
        return False


def trace_hull(bsp: B.Bsp, start: np.ndarray, end: np.ndarray, hull: int = STAND,
               model: int = 0) -> Trace:
    """Move ``hull``'s box from ``start`` to ``end`` through brush model
    ``model``: where it stops and the wall it hit."""
    key = f"hull{hull}:{model}"
    tree = bsp._cache.get(key)
    if not isinstance(tree, _Hull):
        tree = _Hull(bsp, hull, model)
        bsp._cache[key] = tree
    a = np.asarray(start, np.float64)
    b = np.asarray(end, np.float64)
    trace = Trace(end=b.copy())
    if tree.head < 0:
        trace.all_solid = trace.start_solid = tree.contents_of(tree.head) == SOLID
        return trace
    tree.check(tree.head, 0.0, 1.0, a, b, trace)
    if trace.all_solid:
        trace.start_solid = True
    if trace.fraction == 1.0:
        trace.end = b.copy()
    return trace


def segments_clear(bsp: B.Bsp, starts: np.ndarray, ends: np.ndarray, hull: int = STAND,
                   step: float = 8.0) -> np.ndarray:
    """True for each segment (N, 3) -> (N, 3) a ``hull`` box moves along
    without touching anything solid (sampled every ``step`` units)."""
    a = np.atleast_2d(np.asarray(starts, np.float64))
    b = np.atleast_2d(np.asarray(ends, np.float64))
    if not len(a):
        return np.zeros(0, bool)
    length = np.linalg.norm(b - a, axis=1)
    count = np.maximum(np.ceil(length / step).astype(np.int64), 1)
    seg = np.repeat(np.arange(len(a)), count + 1)
    t = np.concatenate([np.linspace(0.0, 1.0, int(n) + 1) for n in count])
    points = a[seg] + (b - a)[seg] * t[:, None]
    ok = point_contents(bsp, points, hull) != SOLID
    clear = np.ones(len(a), bool)
    np.logical_and.at(clear, seg, ok)
    return clear


def fits_player(bsp: B.Bsp, points: np.ndarray, crouch: bool = False) -> np.ndarray:
    """True where a standing (crouching) player's origin is in open space."""
    fits: np.ndarray = point_contents(bsp, points, CROUCH if crouch else STAND) == EMPTY
    return fits


__all__ = ["CONTENTS", "CROUCH", "EMPTY", "HULL_HALF", "LARGE", "LAVA", "SKY", "SLIME", "SOLID",
           "STAND", "Trace", "WATER", "fits_player", "point_contents", "segments_clear",
           "trace_hull"]
