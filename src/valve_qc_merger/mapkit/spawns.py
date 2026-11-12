"""Spawns placed where players can stand and get out: generated, fixed.

:func:`generate` finds room for more ``info_player_start`` (CT) and
``info_player_deathmatch`` (T):

1. **candidates** — a grid (``grid`` units) over the world's floors (faces
   looking up, ``nz >= 0.7``, no sky / water / trigger / clip), each point
   37 units over the floor (a standing player's origin + 1);
2. **room** — hull 1 (a standing player) is empty there and solid 2 units
   lower (it stands on the floor, not in the air); feet and head (hull 0) not
   in water, slime, lava or sky;
3. **entities** — not in the box of a brush entity that hurts, moves or
   breaks (``trigger_hurt``, ``func_door``, ``func_train``,
   ``func_breakable`` …), not within 48 units of another model entity;
4. **reachable** — grid neighbours are joined when a player walks from one
   to the other (up at most 48 over one 48-unit cell — stairs, slopes, a
   jump —, a drop of at most 256, the box free 18 units over the higher end;
   the spots at either end of a ``func_ladder`` are joined; water is walked
   through, never spawned in); only points reached from the map's own
   spawns count (or the largest region when it has none) — skyboxes, roofs
   and closed rooms drop out;
5. **choice** — farthest-point sampling: each new spawn as far as can be
   from every spawn there is (deterministic, no random seed to keep), at
   least ``min_distance`` apart; ``near_existing`` keeps them within
   ``radius`` of the map's spawns;
6. **teams** — CT and T alternate, never on one spot (players would stick);
7. **facing** — the yaw (of 8) with the longest free run ahead.

:func:`fix_stuck` moves a spawn a player does not fit at to the nearest spot
within 32 units that fits, or tells that none does (the fix removes it).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.geometry import geometry
from valve_qc_merger.mapkit.hull import (
    EMPTY,
    LAVA,
    SKY,
    SLIME,
    SOLID,
    STAND,
    WATER,
    point_contents,
    segments_clear,
)
from valve_qc_merger.mapkit.textures import is_special

CT, T = "info_player_start", "info_player_deathmatch"
ORIGIN_UP = 37.0  # over the floor: a standing player's origin (36) + 1
FLOOR_NZ = 0.7
STEP = 18.0  # clearance over the higher end of a move
SWIM = 112.0  # up out of water (a player swims up to the edge)
CLIMB = 48.0  # up between grid neighbours 48 apart: stairs, slopes, a jump (45)
MAX_DROP = 256.0
AVOID = ("trigger_hurt", "trigger_teleport", "trigger_push", "func_water", "func_door",
         "func_door_rotating", "func_train", "func_tracktrain", "func_rotating",
         "func_breakable", "func_pendulum", "func_plat", "func_platrot", "func_vehicle",
         "func_pushable", "momentary_door")
MODEL_CLEARANCE = 48.0
YAWS = tuple(range(0, 360, 45))
LIQUIDS = (WATER, SLIME, LAVA, SKY)


@dataclass
class SpawnOptions:
    per_team: int = 48  # wanted spawns a team, the map's own counted
    grid: float = 48.0
    min_distance: float = 64.0
    spread: str = "uniform"  # or "near_existing"
    radius: float = 1024.0  # near_existing: how far from the map's spawns


@dataclass
class Placed:
    classname: str
    origin: tuple[float, float, float]
    yaw: int

    def entity(self) -> dict[str, str]:
        x, y, z = self.origin
        return {"classname": self.classname, "origin": f"{x:g} {y:g} {z:g}",
                "angles": f"0 {self.yaw} 0"}


@dataclass
class SpawnResult:
    added: list[Placed] = field(default_factory=list)
    candidates: int = 0  # grid points with room
    reachable: int = 0  # of those, reached from the map's spawns
    notes: list[str] = field(default_factory=list)


def _origin(entity: dict[str, str]) -> np.ndarray | None:
    try:
        return np.array([float(v) for v in entity.get("origin", "").split()[:3]], np.float64)
    except ValueError:
        return None


def _floor_points(bsp: B.Bsp, grid: float) -> np.ndarray:
    """Grid points on the world's floors (x, y, floor z)."""
    geo = geometry(bsp)
    faces = geo.model_faces(0)
    normals = geo.face_normals()[faces]
    tex = geo.face_texture()[faces]
    names = [t.name for t in geo.textures]
    out = []
    for face, normal, t in zip(faces, normals, tex, strict=True):
        if normal[2] < FLOOR_NZ or (0 <= t < len(names) and is_special(names[t])):
            continue
        poly = geo.face_points(int(face)).astype(np.float64)
        lo, hi = poly[:, :2].min(axis=0), poly[:, :2].max(axis=0)
        xs = np.arange(math.ceil(lo[0] / grid) * grid, hi[0] + 1e-6, grid)
        ys = np.arange(math.ceil(lo[1] / grid) * grid, hi[1] + 1e-6, grid)
        if not len(xs) or not len(ys):
            continue
        gx, gy = np.meshgrid(xs, ys)
        pts = np.stack([gx.ravel(), gy.ravel()], axis=1)
        inside = np.ones(len(pts), bool)
        nxt = np.roll(poly, -1, axis=0)
        area = float(((poly[:, 0] * nxt[:, 1]) - (nxt[:, 0] * poly[:, 1])).sum())
        if abs(area) < 1e-6:
            continue
        sign = 1.0 if area > 0 else -1.0
        for (ax, ay, _az), (bx, by, _bz) in zip(poly, nxt, strict=True):
            inside &= sign * ((bx - ax) * (pts[:, 1] - ay) - (by - ay) * (pts[:, 0] - ax)) >= -1e-6
        pts = pts[inside]
        if not len(pts):
            continue
        a, b, c = normal
        d = float(normal @ poly[0])
        z = (d - a * pts[:, 0] - b * pts[:, 1]) / c
        out.append(np.column_stack([pts, z]))
    if not out:
        return np.zeros((0, 3))
    points = np.concatenate(out)
    # one point per spot (faces share edges)
    key = np.round(points / [1.0, 1.0, 4.0]).astype(np.int64)
    _, first = np.unique(key, axis=0, return_index=True)
    return points[np.sort(first)]


def _avoid_boxes(bsp: B.Bsp, entities: list[dict[str, str]]) -> list[tuple[np.ndarray,
                                                                           np.ndarray]]:
    geo = geometry(bsp)
    boxes = []
    for entity in entities:
        if entity.get("classname", "") not in AVOID:
            continue
        model = entity.get("model", "")
        if not model.startswith("*"):
            continue
        try:
            index = int(model[1:])
        except ValueError:
            continue
        if not 0 < index < len(geo.models):
            continue
        offset = _origin(entity)
        offset = offset if offset is not None and len(offset) == 3 else np.zeros(3)
        mins, maxs = geo.bounds(index)
        boxes.append((mins + offset - [16, 16, 36], maxs + offset + [16, 16, 36]))
    return boxes


def walk_points(bsp: B.Bsp, entities: list[dict[str, str]], grid: float = 48.0
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Steps 1-3 of the module: player origins a player can stand at
    (water allowed: it walks through), which of them take a spawn (dry,
    clear of hurting / moving brushes and of model entities) and which are
    in water."""
    floor = _floor_points(bsp, grid)
    if not len(floor):
        return floor, np.zeros(0, bool), np.zeros(0, bool)
    origins = floor + [0.0, 0.0, ORIGIN_UP]
    ok = point_contents(bsp, origins, STAND) == EMPTY
    ok &= point_contents(bsp, origins - [0.0, 0.0, 3.0], STAND) == SOLID
    dry = np.ones(len(origins), bool)
    for dz in (-35.0, 0.0, 30.0):  # feet, middle, head
        contents = point_contents(bsp, origins + [0.0, 0.0, dz], 0)
        ok &= ~np.isin(contents, (SLIME, LAVA, SKY))
        dry &= ~np.isin(contents, LIQUIDS)
    origins, dry = origins[ok], dry[ok]
    spawnable = dry.copy()
    for mins, maxs in _avoid_boxes(bsp, entities):
        spawnable &= ~((origins >= mins) & (origins <= maxs)).all(axis=1)
    models = [o for e in entities if e.get("model", "").lower().endswith((".mdl", ".spr"))
              and (o := _origin(e)) is not None and len(o) == 3]
    if models and len(origins):
        spots = np.array(models)
        for chunk in range(0, len(spots), 256):
            d = np.linalg.norm(origins[:, None, :2] - spots[None, chunk:chunk + 256, :2], axis=2)
            spawnable &= (d >= MODEL_CLEARANCE).all(axis=1)
    return origins, spawnable, ~dry


def room_points(bsp: B.Bsp, entities: list[dict[str, str]], grid: float = 48.0
                ) -> np.ndarray:
    """The origins of :func:`walk_points` that take a spawn."""
    origins, spawnable, _wet = walk_points(bsp, entities, grid)
    chosen: np.ndarray = origins[spawnable]
    return chosen


def _ladders(bsp: B.Bsp, entities: list[dict[str, str]]) -> list[tuple[np.ndarray,
                                                                       np.ndarray]]:
    """The boxes a player climbs (``func_ladder``), grown by a grid cell
    sideways and a player's height at the ends."""
    geo = geometry(bsp)
    out = []
    for entity in entities:
        model = entity.get("model", "")
        if entity.get("classname") != "func_ladder" or not model.startswith("*"):
            continue
        try:
            index = int(model[1:])
        except ValueError:
            continue
        if 0 < index < len(geo.models):
            mins, maxs = geo.bounds(index)
            out.append((mins - [56.0, 56.0, 80.0], maxs + [56.0, 56.0, 80.0]))
    return out


def reachable(bsp: B.Bsp, points: np.ndarray, seeds: np.ndarray, grid: float,
              entities: list[dict[str, str]] | None = None,
              wet: np.ndarray | None = None) -> np.ndarray:
    """Step 4: True for each of ``points`` a player walks (or climbs a
    ladder) to from a seed point (or the largest region without seeds)."""
    n = len(points)
    if not n:
        return np.zeros(0, bool)
    cells: dict[tuple[int, int], list[int]] = {}
    keys = np.round(points[:, :2] / grid).astype(np.int64)
    for i, (cx, cy) in enumerate(keys):
        cells.setdefault((int(cx), int(cy)), []).append(i)
    pairs_a, pairs_b = [], []
    for i, (cx, cy) in enumerate(keys):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == dy == 0:
                    continue
                for j in cells.get((int(cx) + dx, int(cy) + dy), ()):
                    dz = points[j, 2] - points[i, 2]
                    climb = SWIM if wet is not None and wet[i] else CLIMB
                    if dz <= climb and dz >= -MAX_DROP:
                        pairs_a.append(i)
                        pairs_b.append(j)
    a = np.array(pairs_a, np.int64)
    b = np.array(pairs_b, np.int64)
    if len(a):
        up = np.maximum(points[a, 2], points[b, 2]) + STEP
        start = points[a].copy()
        start[:, 2] = up
        end = points[b].copy()
        end[:, 2] = up
        ok = segments_clear(bsp, start, end, STAND, 8.0)
        a, b = a[ok], b[ok]
    neighbours: list[list[int]] = [[] for _ in range(n)]
    for i, j in zip(a.tolist(), b.tolist(), strict=True):
        neighbours[i].append(j)
    for mins, maxs in _ladders(bsp, entities or []):
        on = np.nonzero(((points >= mins) & (points <= maxs)).all(axis=1))[0].tolist()
        for i in on:
            neighbours[i] += [j for j in on if j != i]
    seed_index: list[int] = []
    for seed in seeds:
        d = np.linalg.norm(points - seed, axis=1)
        k = int(np.argmin(d))
        if d[k] <= grid * 1.5:
            seed_index.append(k)
    seen = np.zeros(n, bool)
    if seed_index:
        stack = list(set(seed_index))
        seen[stack] = True
        while stack:
            i = stack.pop()
            for j in neighbours[i]:
                if not seen[j]:
                    seen[j] = True
                    stack.append(j)
        return seen
    # no seeds: the largest region, walking both ways
    both: list[set[int]] = [set(x) for x in neighbours]
    for i, js in enumerate(neighbours):
        for j in js:
            both[j].add(i)
    best: list[int] = []
    for first in range(n):
        if seen[first]:
            continue
        region, stack = [first], [first]
        seen[first] = True
        while stack:
            i = stack.pop()
            for j in both[i]:
                if not seen[j]:
                    seen[j] = True
                    stack.append(j)
                    region.append(j)
        if len(region) > len(best):
            best = region
    out = np.zeros(n, bool)
    out[best] = True
    return out


def facing(bsp: B.Bsp, origin: np.ndarray, reach: float = 512.0, step: float = 16.0) -> int:
    """Step 7: the yaw (degrees, of 8) with the longest free run ahead."""
    best, best_run = 0, -1.0
    distances = np.arange(step, reach + step, step)
    for yaw in YAWS:
        direction = np.array([math.cos(math.radians(yaw)), math.sin(math.radians(yaw)), 0.0])
        points = origin + distances[:, None] * direction
        solid = point_contents(bsp, points, STAND) == SOLID
        run = float(distances[np.argmax(solid)] if solid.any() else reach + step)
        if run > best_run:
            best, best_run = yaw, run
    return best


def sample_spots(bsp: B.Bsp, entities: list[dict[str, str]], count: int,
                 options: SpawnOptions | None = None,
                 taken: list[np.ndarray] | None = None) -> SpawnResult:
    """Steps 1-5 and 7: up to ``count`` spots (``Placed`` with no class yet)
    reachable from the map's spawns, far from them, from ``taken`` and
    from each other."""
    options = options or SpawnOptions()
    result = SpawnResult()
    if count <= 0:
        return result
    spawns = [o for e in entities if e.get("classname") in (CT, T)
              and (o := _origin(e)) is not None and len(o) == 3]
    points, spawnable, wet = walk_points(bsp, entities, options.grid)
    result.candidates = int(spawnable.sum())
    seeds = np.array(spawns) if spawns else np.zeros((0, 3))
    if len(seeds):
        fits = point_contents(bsp, seeds + [0.0, 0.0, 1.0], STAND) == EMPTY
        seeds = seeds[fits] if fits.any() else seeds
    reach = reachable(bsp, points, seeds, options.grid, entities, wet)
    points = points[reach & spawnable]
    result.reachable = len(points)
    if options.spread == "near_existing" and len(seeds) and len(points):
        d = np.min(np.linalg.norm(points[:, None] - seeds[None], axis=2), axis=1)
        points = points[d <= options.radius]
    avoid = spawns + list(taken or [])
    nearest = (np.min(np.linalg.norm(points[:, None] - np.array(avoid)[None], axis=2), axis=1)
               if avoid and len(points) else np.full(len(points), np.inf))
    while len(result.added) < count and len(points):
        k = int(np.argmax(nearest))
        if nearest[k] < options.min_distance:
            break
        origin = points[k]
        spot = (round(float(origin[0]), 1), round(float(origin[1]), 1),
                round(float(origin[2]), 1))
        result.added.append(Placed("", spot, facing(bsp, origin)))
        nearest = np.minimum(nearest, np.linalg.norm(points - origin, axis=1))
        nearest[k] = -1.0
    if len(result.added) < count:
        result.notes.append(f"room for {len(result.added)} of {count} (reachable spots "
                            f"{options.min_distance:g} units apart)")
    return result


def generate(bsp: B.Bsp, entities: list[dict[str, str]], options: SpawnOptions | None = None
             ) -> SpawnResult:
    """New spawns until each team has ``options.per_team`` (see the module)."""
    options = options or SpawnOptions()
    counts = {CT: sum(e.get("classname") == CT for e in entities),
              T: sum(e.get("classname") == T for e in entities)}
    left = {team: max(options.per_team - counts[team], 0) for team in (CT, T)}
    result = sample_spots(bsp, entities, left[CT] + left[T], options)
    turn = CT if left[CT] >= left[T] else T
    for placed in result.added:
        if not left[turn]:
            turn = T if turn == CT else CT
        placed.classname = turn
        left[turn] -= 1
        turn = T if turn == CT else CT
    return result


def fix_stuck(bsp: B.Bsp, origin: tuple[float, float, float], reach: float = 32.0
              ) -> tuple[float, float, float] | None:
    """The nearest spot within ``reach`` where a standing player fits on a
    floor (None: no spot; the spawn is removed)."""
    o = np.array(origin, np.float64)
    steps = np.arange(-reach, reach + 1e-6, 4.0)
    gx, gy, gz = np.meshgrid(steps, steps, np.arange(-reach, reach + 1e-6, 2.0), indexing="ij")
    offsets = np.stack([gx.ravel(), gy.ravel(), gz.ravel()], axis=1)
    offsets = offsets[np.linalg.norm(offsets, axis=1) <= reach]
    offsets = offsets[np.argsort(np.linalg.norm(offsets, axis=1), kind="stable")]
    lifted = o + offsets + [0.0, 0.0, 1.0]
    fits = point_contents(bsp, lifted, STAND) == EMPTY
    fits &= point_contents(bsp, lifted - [0.0, 0.0, 64.0], STAND) == SOLID  # a floor below
    if not fits.any():
        return None
    k = int(np.argmax(fits))
    x, y, z = o + offsets[k]
    return round(float(x), 1), round(float(y), 1), round(float(z), 1)


__all__ = ["CT", "Placed", "SpawnOptions", "SpawnResult", "T", "facing", "fix_stuck",
           "generate", "reachable", "room_points", "sample_spots", "walk_points"]
