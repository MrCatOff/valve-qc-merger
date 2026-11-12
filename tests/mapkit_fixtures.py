"""Small valid BSP v30 maps built in memory for the mapkit tests.

The world is a set of solid axis-aligned boxes (walls, floor, ceiling)
inside a sealed outer box: hull 0 (nodes / leafs) and the player hulls
(clipnodes, each box grown by the hull's half size) are real BSP trees, so
the engine's walk finds the same contents a compiled map gives. Faces are
the floors (and whatever a test adds); textures: two embedded, one
external.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.hull import HULL_HALF
from valve_qc_merger.sprite.wad import Texture, miptex_lump

Box = tuple[tuple[float, float, float], tuple[float, float, float]]
SOLID, EMPTY = -2, -1

# a 1024 x 512 room, 256 high, a wall across it at x = 0 with a doorway at y > 128
ROOM: Box = ((-512.0, -256.0, 0.0), (512.0, 256.0, 256.0))
WALL: Box = ((-8.0, -256.0, 0.0), (8.0, 128.0, 256.0))


def room_solids(room: Box = ROOM, walls: list[Box] | None = None, thick: float = 64.0
                ) -> tuple[list[Box], Box]:
    """The solid boxes of a sealed room (floor, ceiling, four walls) plus
    ``walls``; and the outer box (everything outside it is solid)."""
    (x0, y0, z0), (x1, y1, z1) = room
    t = thick
    solids: list[Box] = [
        ((x0 - t, y0 - t, z0 - t), (x1 + t, y1 + t, z0)),  # floor
        ((x0 - t, y0 - t, z1), (x1 + t, y1 + t, z1 + t)),  # ceiling
        ((x0 - t, y0 - t, z0), (x0, y1 + t, z1)),
        ((x1, y0 - t, z0), (x1 + t, y1 + t, z1)),
        ((x0, y0 - t, z0), (x1, y0, z1)),
        ((x0, y1, z0), (x1, y1 + t, z1)),
    ]
    outer: Box = ((x0 - t, y0 - t, z0 - t), (x1 + t, y1 + t, z1 + t))
    return solids + list(walls if walls is not None else [WALL]), outer


@dataclass
class _Tree:
    planes: list[tuple[int, float]] = field(default_factory=list)  # (axis, dist)
    nodes: list[tuple[int, int, int]] = field(default_factory=list)  # plane, front, back

    def plane(self, axis: int, dist: float) -> int:
        key = (axis, float(dist))
        if key not in self.planes:
            self.planes.append(key)
        return self.planes.index(key)


def _build(tree: _Tree, region: Box, solids: list[Box], outer: Box) -> int:
    """A node index (>= 0) or a contents value (< 0) for ``region``."""
    lo, hi = np.array(region[0]), np.array(region[1])
    olo, ohi = np.array(outer[0]), np.array(outer[1])
    if (hi <= olo).any() or (lo >= ohi).any():
        return SOLID  # outside the sealed world
    for axis in range(3):  # cut down to the outer box first
        if lo[axis] < olo[axis] < hi[axis]:
            return _split(tree, region, solids, outer, axis, olo[axis])
        if lo[axis] < ohi[axis] < hi[axis]:
            return _split(tree, region, solids, outer, axis, ohi[axis])
    touching = []
    for smin, smax in solids:
        smin, smax = np.array(smin), np.array(smax)
        if (smax <= lo).any() or (smin >= hi).any():
            continue
        if (smin <= lo).all() and (smax >= hi).all():
            return SOLID
        touching.append((smin, smax))
    if not touching:
        return EMPTY
    for smin, smax in touching:
        for axis in range(3):
            for value in (smin[axis], smax[axis]):
                if lo[axis] < value < hi[axis]:
                    return _split(tree, region, solids, outer, axis, value)
    return SOLID  # pragma: no cover - a touching box always cuts or covers


def _split(tree: _Tree, region: Box, solids: list[Box], outer: Box, axis: int,
           value: float) -> int:
    lo, hi = list(region[0]), list(region[1])
    front_lo = list(lo)
    front_lo[axis] = value
    back_hi = list(hi)
    back_hi[axis] = value
    index = len(tree.nodes)
    tree.nodes.append((tree.plane(axis, value), 0, 0))
    front = _build(tree, (tuple(front_lo), tuple(hi)), solids, outer)  # type: ignore[arg-type]
    back = _build(tree, (tuple(lo), tuple(back_hi)), solids, outer)  # type: ignore[arg-type]
    tree.nodes[index] = (tree.nodes[index][0], front, back)
    return index


def _grow(box: Box, half: tuple[float, float, float]) -> Box:
    (a, b, c), (d, e, f) = box
    return (a - half[0], b - half[1], c - half[2]), (d + half[0], e + half[1], f + half[2])


@dataclass
class Face:
    points: list[tuple[float, float, float]]
    texture: int = 0
    scale: float = 1.0  # texture units per world unit = 1 / scale
    special: bool = False


def floor_faces(room: Box = ROOM, texture: int = 0) -> list[Face]:
    (x0, y0, z0), (x1, y1, _z1) = room
    mid = (x0 + x1) / 2
    return [Face([(x0, y0, z0), (mid, y0, z0), (mid, y1, z0), (x0, y1, z0)], texture, 4.0),
            Face([(mid, y0, z0), (x1, y0, z0), (x1, y1, z0), (mid, y1, z0)], texture + 1, 4.0)]


def texture(name: str, colour: tuple[int, int, int], size: int = 16) -> Texture:
    return Texture(name, size, size, bytes(size * size), [colour] + [(0, 0, 0)] * 255)


def build_map(entities: list[dict[str, str]] | None = None, *, solids: list[Box] | None = None,
              outer: Box | None = None, faces: list[Face] | None = None,
              embedded: list[Texture] | None = None, external: list[tuple[str, int, int]] | None
              = None, visibility: bytes = b"\x01", lighting: bytes = b"\x80" * 3,
              extra_models: int = 0) -> bytes:
    """A BSP v30 map (see the module)."""
    if solids is None or outer is None:
        solids, outer = room_solids()
    entities = entities if entities is not None else [{"classname": "worldspawn"}]
    faces = faces if faces is not None else floor_faces()
    embedded = embedded if embedded is not None else [texture("floor_a", (200, 60, 40)),
                                                       texture("floor_b", (40, 160, 60))]
    external = external if external is not None else [("wall_ext", 64, 64)]
    big = ((outer[0][0] - 64, outer[0][1] - 64, outer[0][2] - 64),
           (outer[1][0] + 64, outer[1][1] + 64, outer[1][2] + 64))

    tree = _Tree()
    roots = []
    trees: list[list[tuple[int, int, int]]] = []
    for hull in (0, 1, 2, 3):
        half = (0.0, 0.0, 0.0) if hull == 0 else HULL_HALF[hull]
        grown = [_grow(s, half) for s in solids]
        start = len(tree.nodes)
        root = _build(tree, big, grown, outer)
        trees.append(tree.nodes[start:])
        roots.append((root, start))
    # hull 0 -> nodes / leafs (leaf 0 solid, leaf 1 empty); 1..3 -> clipnodes
    nodes_raw, clip_raw = b"", b""
    heads = []
    clip_count = 0
    for hull, ((root, start), sub) in enumerate(zip(roots, trees, strict=True)):
        def ref(value: int, base: int, start: int = start, hull: int = hull) -> int:
            if value >= 0:
                return value - start + base
            return (-1 - (0 if value == SOLID else 1)) if hull == 0 else value
        if hull == 0:
            for plane, front, back in sub:
                nodes_raw += struct.pack("<i2h3h3h2H", plane, ref(front, 0), ref(back, 0),
                                         -4096, -4096, -4096, 4096, 4096, 4096, 0, 0)
            heads.append(ref(root, 0) if root >= 0 else 0)
        else:
            for plane, front, back in sub:
                clip_raw += struct.pack("<i2h", plane, ref(front, clip_count),
                                        ref(back, clip_count))
            heads.append(ref(root, clip_count) if root >= 0 else root)
            clip_count += len(sub)
    leafs = (struct.pack("<2i3h3h2H4B", SOLID, -1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
             + struct.pack("<2i3h3h2H4B", EMPTY, -1, -4096, -4096, -4096, 4096, 4096, 4096,
                           0, 0, 0, 0, 0, 0))

    # faces
    planes = list(tree.planes)
    verts: list[tuple[float, float, float]] = []
    edges = [(0, 0)]
    surfedges: list[int] = []
    texinfo = b""
    faces_raw = b""
    for face in faces:
        pts = np.array(face.points, np.float64)
        normal = np.cross(pts[1] - pts[0], pts[2] - pts[0])
        axis = int(np.argmax(np.abs(normal)))
        side = 0 if normal[axis] > 0 else 1
        key = (axis, float(pts[0][axis]))
        if key not in planes:
            planes.append(key)
        first, base = len(surfedges), len(verts)
        verts += face.points
        for k in range(len(face.points)):
            edges.append((base + k, base + (k + 1) % len(face.points)))
            surfedges.append(len(edges) - 1)
        s_axis, t_axis = [(1, 2), (0, 2), (0, 1)][axis]
        vecs = np.zeros((2, 4))
        vecs[0, s_axis] = 1.0 / face.scale
        vecs[1, t_axis] = -1.0 / face.scale
        texinfo += struct.pack("<8f2i", *vecs.ravel(), face.texture, 1 if face.special else 0)
        faces_raw += struct.pack("<2Hi2H4Bi", planes.index(key), side, first, len(face.points),
                                 len(texinfo) // 40 - 1, 0, 255, 255, 255,
                                 -1 if face.special else 0)
    planes_raw = b"".join(struct.pack("<4fi", *[1.0 if i == a else 0.0 for i in range(3)], d, a)
                          for a, d in planes)
    # textures
    blobs = [miptex_lump(t) for t in embedded]
    blobs += [struct.pack("<16s2I4I", n.encode()[:15].ljust(16, b"\0"), w, h, 0, 0, 0, 0)
              for n, w, h in external]
    offsets, position = [], 4 + 4 * len(blobs)
    for blob in blobs:
        offsets.append(position)
        position += len(blob)
    textures_raw = struct.pack(f"<i{len(blobs)}i", len(blobs), *offsets) + b"".join(blobs)
    (ox0, oy0, oz0), (ox1, oy1, oz1) = outer
    models = struct.pack("<9f4i3i", ox0, oy0, oz0, ox1, oy1, oz1, 0, 0, 0, *heads, 1, 0,
                         len(faces))
    for _k in range(extra_models):
        models += struct.pack("<9f4i3i", -8, -8, -8, 8, 8, 8, 0, 0, 0, 0, SOLID, SOLID, SOLID,
                              0, 0, 0)
    from valve_qc_merger.server.entities import entity_text
    lumps = [b""] * B.LUMPS
    lumps[B.ENTITIES] = entity_text(entities).encode("latin-1") + b"\0"
    lumps[B.PLANES] = planes_raw
    lumps[B.TEXTURES] = textures_raw
    lumps[B.VERTEXES] = b"".join(struct.pack("<3f", *v) for v in verts)
    lumps[B.VISIBILITY] = visibility
    lumps[B.NODES] = nodes_raw
    lumps[B.TEXINFO] = texinfo
    lumps[B.FACES] = faces_raw
    lumps[B.LIGHTING] = lighting
    lumps[B.CLIPNODES] = clip_raw
    lumps[B.LEAFS] = leafs
    lumps[B.MARKSURFACES] = b""
    lumps[B.EDGES] = b"".join(struct.pack("<2H", *e) for e in edges)
    lumps[B.SURFEDGES] = struct.pack(f"<{len(surfedges)}i", *surfedges)
    lumps[B.MODELS] = models
    body = bytearray(B.HEADER)
    table = []
    for lump in lumps:
        while len(body) % 4:
            body += b"\0"
        table.append((len(body), len(lump)))
        body += lump
    body[:B.HEADER] = struct.pack("<i", B.VERSION) + b"".join(struct.pack("<2i", *t)
                                                              for t in table)
    return bytes(body)


__all__ = ["Face", "ROOM", "WALL", "build_map", "floor_faces", "room_solids", "texture"]
