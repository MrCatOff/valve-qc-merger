"""What is at a point of a map: empty, solid, water … for a hull.

The compiler builds the world four times: hull 0 for points (``nodes`` and
``leafs``), and hulls 1-3 with the walls pushed out by a box (``clipnodes``)
— 1 a standing player (±16 × ±16 × ±36), 2 a large monster, 3 a crouching
player (±16 × ±16 × ±18). "Does a standing player fit with its origin
here?" is then one walk down hull 1 for the point itself. Points go in
batches (numpy), every point a walk of its own.
"""

from __future__ import annotations

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


def fits_player(bsp: B.Bsp, points: np.ndarray, crouch: bool = False) -> np.ndarray:
    """True where a standing (crouching) player's origin is in open space."""
    fits: np.ndarray = point_contents(bsp, points, CROUCH if crouch else STAND) == EMPTY
    return fits


__all__ = ["CONTENTS", "CROUCH", "EMPTY", "HULL_HALF", "LARGE", "LAVA", "SKY", "SLIME", "SOLID",
           "STAND", "WATER", "fits_player", "point_contents"]
