"""The parsed lumps of a map as numpy arrays.

``Geometry(bsp)`` reads planes, vertices, edges, faces, texinfo, nodes,
clipnodes, leafs and brush models once; each face's polygon is
``face_points(i)`` (its surfedges walked in order). The engine's checks
that matter for loading — surface extents, world bounds — are computed
here the way it computes them.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from valve_qc_merger.mapkit import bsp as B

TEX_SPECIAL = 1  # texinfo flag: sky, water, aaatrigger … (no lightmap)
MAX_EXTENT = 256  # texels (16 luxels): the engine stops on a bigger lit face
WORLD_LIMIT = 4096.0

PLANE = np.dtype([("normal", "<f4", 3), ("dist", "<f4"), ("type", "<i4")])
TEXINFO = np.dtype([("vecs", "<f4", (2, 4)), ("miptex", "<i4"), ("flags", "<i4")])
FACE = np.dtype([("plane", "<u2"), ("side", "<u2"), ("firstedge", "<i4"), ("numedges", "<u2"),
                 ("texinfo", "<u2"), ("styles", "u1", 4), ("lightofs", "<i4")])
NODE = np.dtype([("plane", "<i4"), ("children", "<i2", 2), ("mins", "<i2", 3),
                 ("maxs", "<i2", 3), ("firstface", "<u2"), ("numfaces", "<u2")])
CLIPNODE = np.dtype([("plane", "<i4"), ("children", "<i2", 2)])
LEAF = np.dtype([("contents", "<i4"), ("visofs", "<i4"), ("mins", "<i2", 3),
                 ("maxs", "<i2", 3), ("firstmark", "<u2"), ("nummarks", "<u2"),
                 ("ambient", "u1", 4)])
MODEL = np.dtype([("mins", "<f4", 3), ("maxs", "<f4", 3), ("origin", "<f4", 3),
                  ("headnode", "<i4", 4), ("visleafs", "<i4"), ("firstface", "<i4"),
                  ("numfaces", "<i4")])


@dataclass
class MipTex:
    """One entry of the textures lump."""

    index: int
    name: str
    width: int
    height: int
    embedded: bool  # pixels in the BSP (else looked up by name in the WADs)
    offset: int  # within the textures lump, -1 for an empty slot


def _array(data: bytes, dtype: np.dtype) -> np.ndarray:
    count = len(data) // dtype.itemsize
    return np.frombuffer(data[:count * dtype.itemsize], dtype)


def read_miptex_table(lump: bytes) -> list[MipTex]:
    if len(lump) < 4:
        return []
    (count,) = struct.unpack_from("<i", lump, 0)
    count = max(0, min(count, (len(lump) - 4) // 4))
    offsets = struct.unpack_from(f"<{count}i", lump, 4)
    out = []
    for index, offset in enumerate(offsets):
        if offset < 0 or offset + 40 > len(lump):
            out.append(MipTex(index, "", 0, 0, False, -1))
            continue
        raw, width, height, *mips = struct.unpack_from("<16s2I4I", lump, offset)
        name = raw.split(b"\0", 1)[0].decode("latin-1")
        out.append(MipTex(index, name, width, height, mips[0] != 0, offset))
    return out


class Geometry:
    """Arrays of one map's lumps (read-only views of its bytes)."""

    def __init__(self, bsp: B.Bsp) -> None:
        lumps = bsp.lumps
        self.planes = _array(lumps[B.PLANES], PLANE)
        self.vertices = _array(lumps[B.VERTEXES], np.dtype("<f4")).reshape(-1, 3)
        self.edges = _array(lumps[B.EDGES], np.dtype("<u2")).reshape(-1, 2)
        self.surfedges = _array(lumps[B.SURFEDGES], np.dtype("<i4"))
        self.faces = _array(lumps[B.FACES], FACE)
        self.texinfo = _array(lumps[B.TEXINFO], TEXINFO)
        self.nodes = _array(lumps[B.NODES], NODE)
        self.clipnodes = _array(lumps[B.CLIPNODES], CLIPNODE)
        self.leafs = _array(lumps[B.LEAFS], LEAF)
        self.models = _array(lumps[B.MODELS], MODEL)
        self.textures = read_miptex_table(lumps[B.TEXTURES])
        self._face_vertex: np.ndarray | None = None

    # -- faces --------------------------------------------------------------

    def face_vertex_indices(self) -> np.ndarray:
        """Every surfedge's start vertex (index into ``vertices``): face i's
        polygon is ``[firstedge, firstedge + numedges)`` of this array."""
        if self._face_vertex is None:
            se = self.surfedges.astype(np.int64)
            edges = self.edges[np.abs(se)]
            self._face_vertex = np.where(se >= 0, edges[:, 0], edges[:, 1]).astype(np.int64)
        return self._face_vertex

    def face_points(self, face: int) -> np.ndarray:
        f = self.faces[face]
        start = int(f["firstedge"])
        index = self.face_vertex_indices()[start:start + int(f["numedges"])]
        points: np.ndarray = self.vertices[index]
        return points

    def face_normals(self) -> np.ndarray:
        normals = self.planes["normal"][self.faces["plane"]].astype(np.float64)
        flipped: np.ndarray = np.where((self.faces["side"] != 0)[:, None], -normals, normals)
        return flipped

    def face_texture(self) -> np.ndarray:
        """The miptex index of every face (-1: no texinfo)."""
        ti = self.faces["texinfo"].astype(np.int64)
        ok = ti < len(self.texinfo)
        out = np.full(len(self.faces), -1, np.int64)
        out[ok] = self.texinfo["miptex"][ti[ok]]
        return out

    def face_areas(self) -> np.ndarray:
        """The area of every face (square units)."""
        out = np.zeros(len(self.faces))
        if not len(self.faces):
            return out
        index = self.face_vertex_indices()
        counts = self.faces["numedges"].astype(np.int64)
        starts = self.faces["firstedge"].astype(np.int64)
        for k in range(1, int(np.max(counts)) - 1):
            has = counts > k + 1
            a = self.vertices[index[starts[has]]].astype(np.float64)
            b = self.vertices[index[starts[has] + k]].astype(np.float64)
            c = self.vertices[index[starts[has] + k + 1]].astype(np.float64)
            out[has] += 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
        return out

    def model_faces(self, model: int = 0) -> np.ndarray:
        m = self.models[model]
        return np.arange(int(m["firstface"]), int(m["firstface"]) + int(m["numfaces"]))

    def surface_extents(self) -> np.ndarray:
        """(faces, 2) texel extents as the engine's ``CalcSurfaceExtents``:
        the face's texture coordinates rounded out to 16, max - min."""
        index = self.face_vertex_indices()
        out = np.zeros((len(self.faces), 2), np.int64)
        if not len(self.faces):
            return out
        vecs = self.texinfo["vecs"][np.minimum(self.faces["texinfo"], len(self.texinfo) - 1)]
        counts = self.faces["numedges"].astype(np.int64)
        face_of = np.repeat(np.arange(len(self.faces)), counts)
        starts = self.faces["firstedge"].astype(np.int64)
        offsets = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
        points = self.vertices[index[np.repeat(starts, counts) + offsets]].astype(np.float64)
        v = vecs[face_of].astype(np.float64)
        for axis in range(2):
            st = (points * v[:, axis, :3]).sum(axis=1) + v[:, axis, 3]
            low = np.full(len(self.faces), np.inf)
            high = np.full(len(self.faces), -np.inf)
            np.minimum.at(low, face_of, st)
            np.maximum.at(high, face_of, st)
            out[:, axis] = (np.ceil(high / 16) - np.floor(low / 16)).astype(np.int64) * 16
        return out

    def bad_extent_faces(self) -> np.ndarray:
        """Lit faces (no ``TEX_SPECIAL``) wider than 256 texels: the client
        stops with "Bad surface extents" loading the map."""
        if not len(self.faces) or not len(self.texinfo):
            return np.zeros(0, np.int64)
        flags = self.texinfo["flags"][np.minimum(self.faces["texinfo"], len(self.texinfo) - 1)]
        extents = self.surface_extents()
        bad = ((flags & TEX_SPECIAL) == 0) & (extents.max(axis=1) > MAX_EXTENT)
        found: np.ndarray = np.nonzero(bad)[0]
        return found

    def bounds(self, model: int = 0) -> tuple[np.ndarray, np.ndarray]:
        m = self.models[model]
        return m["mins"].astype(np.float64), m["maxs"].astype(np.float64)


def geometry(bsp: B.Bsp) -> Geometry:
    cached = bsp._cache.get("geometry")
    if not isinstance(cached, Geometry):
        cached = Geometry(bsp)
        bsp._cache["geometry"] = cached
    return cached


__all__ = ["CLIPNODE", "FACE", "Geometry", "LEAF", "MAX_EXTENT", "MODEL", "MipTex", "NODE",
           "PLANE", "TEXINFO", "TEX_SPECIAL", "WORLD_LIMIT", "geometry", "read_miptex_table"]
