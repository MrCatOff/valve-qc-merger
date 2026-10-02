"""Fold a weapon's always-on parts into ONE submodel, simplifying if needed.

GoldSource caps a submodel at 2048 vertices (studiomdl's ``MAXSTUDIOVERTS``,
and the engine's transform arrays): a vertex is a unique (bone, position)
pair, normals are counted the same way. A multi-part weapon whose parts
together exceed that cannot become one submodel as is; under
``--shared-hands`` that used to reject it.

:func:`fold_parts` concatenates the parts (bones matched by name) and, when
the result is over budget, removes vertices by **half-edge collapses**
ranked by quadric error (Garland-Heckbert): a vertex ``u`` is merged INTO a
neighbour ``w`` that already exists, so no new position is ever created.
A collapse is refused when it would

- join vertices skinned to different bones (the animation would change),
- move a UV-seam, material-border or mesh-boundary vertex (textures would
  smear, holes would open),
- flip or degenerate a triangle, or break the mesh's local topology.

It gives up (returns ``None``) if more than ``max_fraction`` of the
vertices would have to go. The report says how many were removed and how
far the farthest one moved.
"""

from __future__ import annotations

import dataclasses
import heapq
from dataclasses import dataclass

import numpy as np

from valve_qc_merger.models.smd import Smd, Triangle, Vertex

VERTEX_BUDGET = 2048
# Vertex identity is the EXACT (bone, position) pair, as studiomdl compares
# (and as merge-v's budget check counts): rounding would merge near-twins
# that the compiler keeps apart, landing a "2048" mesh at 2050.


@dataclass
class FoldReport:
    parts: int
    vertices_before: int
    vertices_after: int
    normals_after: int
    max_shift: float  # farthest a removed vertex moved along the mesh (units)
    surface_error: float  # farthest a removed vertex lies from the new surface

    @property
    def removed_fraction(self) -> float:
        if not self.vertices_before:
            return 0.0
        return 1.0 - self.vertices_after / self.vertices_before


def unique_vertices(smd: Smd) -> int:
    return len({(v.bone, v.position) for t in smd.triangles for v in t.vertices})


def unique_normals(smd: Smd) -> int:
    return len({(v.bone, v.normal) for t in smd.triangles for v in t.vertices})


def concat_by_name(meshes: list[Smd]) -> Smd | None:
    """One SMD from several parts of the same model: vertices are re-bound to
    the fullest part's node table by bone NAME (None if a bone is missing)."""
    base = max(meshes, key=lambda m: len(m.nodes))
    index_of = {n.name: n.index for n in base.nodes}
    triangles: list[Triangle] = []
    for mesh in meshes:
        remap: dict[int, int] = {}
        for node in mesh.nodes:
            if node.name not in index_of:
                return None
            remap[node.index] = index_of[node.name]
        for t in mesh.triangles:
            triangles.append(Triangle(t.material, tuple(
                dataclasses.replace(v, bone=remap[v.bone]) for v in t.vertices)))  # type: ignore[arg-type]
    return Smd(nodes=list(base.nodes), frames=list(base.frames), triangles=triangles)


# --------------------------------------------------------------------------- #
# Half-edge collapse decimation
# --------------------------------------------------------------------------- #
def _plane_quadric(p0: np.ndarray, p1: np.ndarray, p2: np.ndarray) -> np.ndarray:
    n = np.cross(p1 - p0, p2 - p0)
    area = np.linalg.norm(n)
    if area < 1e-12:
        return np.zeros((4, 4))
    n = n / area
    plane = np.append(n, -np.dot(n, p0))
    return np.outer(plane, plane) * (area * 0.5)


def decimate(smd: Smd, target: int,
             max_fraction: float) -> tuple[Smd, float, np.ndarray] | None:
    """Collapse vertices until ``unique_vertices <= target``; None if that
    needs more than ``max_fraction`` of them or the constraints run out.
    Returns the new mesh, the largest distance a removed vertex moved, and
    the removed vertices' original positions."""
    key_of: dict[tuple, int] = {}
    pos: list[np.ndarray] = []
    bone: list[int] = []
    tris: list[list[int]] = []  # vertex ids per triangle (None-free while alive)
    corners: list[tuple[Vertex, Vertex, Vertex]] = []
    materials: list[str] = []
    for t in smd.triangles:
        ids = []
        for v in t.vertices:
            key = (v.bone, v.position)
            vid = key_of.get(key)
            if vid is None:
                vid = len(pos)
                key_of[key] = vid
                pos.append(np.array(tuple(v.position), dtype=float))
                bone.append(v.bone)
            ids.append(vid)
        tris.append(ids)
        corners.append(t.vertices)
        materials.append(t.material)

    count = len(pos)
    if count <= target:
        return smd, 0.0, np.zeros((0, 3))
    needed = count - target
    if needed > max_fraction * count:
        return None

    alive_tri = [len(set(ids)) == 3 for ids in tris]
    vtris: list[set[int]] = [set() for _ in range(count)]
    for ti, ids in enumerate(tris):
        if alive_tri[ti]:
            for vid in ids:
                vtris[vid].add(ti)

    # constraints: seam (several UVs/normals/materials at one vertex) and boundary
    uv_sets: list[set] = [set() for _ in range(count)]
    mat_sets: list[set] = [set() for _ in range(count)]
    for ti, ids in enumerate(tris):
        for c, vid in enumerate(ids):
            vx = corners[ti][c]
            uv_sets[vid].add((round(vx.uv.u, 4), round(vx.uv.v, 4)))
            mat_sets[vid].add(materials[ti])
    edge_count: dict[tuple[int, int], int] = {}
    for ti, ids in enumerate(tris):
        if not alive_tri[ti]:
            continue
        for a, b in ((ids[0], ids[1]), (ids[1], ids[2]), (ids[2], ids[0])):
            e = (a, b) if a < b else (b, a)
            edge_count[e] = edge_count.get(e, 0) + 1
    fixed = [len(uv_sets[v]) > 1 or len(mat_sets[v]) > 1 for v in range(count)]
    for (a, b), n in edge_count.items():
        if n != 2:  # boundary or non-manifold edge
            fixed[a] = fixed[b] = True

    quad = [np.zeros((4, 4)) for _ in range(count)]
    for ti, ids in enumerate(tris):
        if alive_tri[ti]:
            q = _plane_quadric(pos[ids[0]], pos[ids[1]], pos[ids[2]])
            for vid in ids:
                quad[vid] += q

    def neighbours(v: int) -> set[int]:
        out: set[int] = set()
        for ti in vtris[v]:
            out.update(tris[ti])
        out.discard(v)
        return out

    def cost(u: int, w: int) -> float:
        p = np.append(pos[w], 1.0)
        return float(p @ (quad[u] + quad[w]) @ p)

    version = [0] * count
    heap: list[tuple[float, int, int, int]] = []

    def push(u: int) -> None:
        if fixed[u] or not vtris[u]:
            return
        best = None
        for w in neighbours(u):
            if bone[w] != bone[u]:
                continue
            c = cost(u, w)
            if best is None or c < best[0]:
                best = (c, w)
        if best is not None:
            heapq.heappush(heap, (best[0], u, best[1], version[u]))

    for u in range(count):
        push(u)

    removed = 0
    max_shift = 0.0
    removed_points: list[np.ndarray] = []
    alive_v = [True] * count
    while removed < needed and heap:
        _c, u, w, ver = heapq.heappop(heap)
        if not alive_v[u] or not alive_v[w] or ver != version[u] or fixed[u]:
            continue
        # link condition (manifold-preserving): shared neighbours == 2
        shared_tris = vtris[u] & vtris[w]
        if len(shared_tris) != 2 or len(neighbours(u) & neighbours(w)) != 2:
            version[u] += 1
            continue
        # no flips / degenerates among the triangles that keep u -> w
        ok = True
        for ti in vtris[u] - shared_tris:
            ids = tris[ti]
            old = np.cross(pos[ids[1]] - pos[ids[0]], pos[ids[2]] - pos[ids[0]])
            moved = [pos[w] if vid == u else pos[vid] for vid in ids]
            new = np.cross(moved[1] - moved[0], moved[2] - moved[0])
            if np.linalg.norm(new) < 1e-9 or np.dot(old, new) <= 0.2 * np.linalg.norm(old) \
                    * np.linalg.norm(new):
                ok = False
                break
        if not ok:
            version[u] += 1
            continue

        # w's corner attributes on the collapsing side (u is not a seam, so
        # its region is one UV island; take w's UV/normal from a shared tri)
        donor_ti = next(iter(shared_tris))
        donor = corners[donor_ti][tris[donor_ti].index(w)]
        for ti in shared_tris:
            alive_tri[ti] = False
            for vid in tris[ti]:
                vtris[vid].discard(ti)
        for ti in list(vtris[u]):
            ids = tris[ti]
            c = ids.index(u)
            ids[c] = w
            cs = list(corners[ti])
            cs[c] = dataclasses.replace(cs[c], position=donor.position, normal=donor.normal,
                                        uv=donor.uv, bone=donor.bone)
            corners[ti] = tuple(cs)  # type: ignore[assignment]
            vtris[w].add(ti)
        vtris[u] = set()
        alive_v[u] = False
        quad[w] = quad[w] + quad[u]
        max_shift = max(max_shift, float(np.linalg.norm(pos[u] - pos[w])))
        removed_points.append(pos[u])
        removed += 1
        for v in neighbours(w) | {w}:
            version[v] += 1
            push(v)

    if removed < needed:
        return None
    out = Smd(nodes=list(smd.nodes), frames=list(smd.frames), triangles=[
        Triangle(materials[ti], corners[ti]) for ti in range(len(tris)) if alive_tri[ti]
    ])
    return out, max_shift, np.array(removed_points).reshape(-1, 3)


def surface_distance(points: np.ndarray, smd: Smd) -> float:
    """Largest distance from ``points`` to the mesh surface (exact point-to-
    triangle distance, numpy-vectorised over the triangles)."""
    if len(points) == 0 or not smd.triangles:
        return 0.0
    tri = np.array([[tuple(v.position) for v in t.vertices] for t in smd.triangles])
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    worst = 0.0
    for p in points:
        worst = max(worst, float(np.min(_point_triangle_distance(p, a, b, c))))
    return worst


def _point_triangle_distance(p: np.ndarray, a: np.ndarray, b: np.ndarray,
                             c: np.ndarray) -> np.ndarray:
    """Distance from one point to many triangles (Ericson, Real-Time
    Collision Detection 5.1.5, vectorised)."""
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = np.einsum("ij,ij->i", ab, ap), np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3, d4 = np.einsum("ij,ij->i", ab, bp), np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5, d6 = np.einsum("ij,ij->i", ab, cp), np.einsum("ij,ij->i", ac, cp)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    denom = va + vb + vc
    denom = np.where(np.abs(denom) < 1e-12, 1e-12, denom)
    v = vb / denom
    w = vc / denom
    closest = a + ab * v[:, None] + ac * w[:, None]  # interior
    # vertex / edge regions
    def pick(mask, value):
        nonlocal closest
        closest = np.where(mask[:, None], value, closest)
    with np.errstate(divide="ignore", invalid="ignore"):
        t_ab = np.clip(d1 / np.where(d1 - d3 == 0, 1, d1 - d3), 0, 1)
        t_ac = np.clip(d2 / np.where(d2 - d6 == 0, 1, d2 - d6), 0, 1)
        t_bc = np.clip((d4 - d3) / np.where((d4 - d3) + (d5 - d6) == 0, 1,
                                            (d4 - d3) + (d5 - d6)), 0, 1)
    pick((vc <= 0) & (d1 >= 0) & (d3 <= 0), a + ab * t_ab[:, None])
    pick((vb <= 0) & (d2 >= 0) & (d6 <= 0), a + ac * t_ac[:, None])
    pick((va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0), b + (c - b) * t_bc[:, None])
    pick((d1 <= 0) & (d2 <= 0), a)
    pick((d3 >= 0) & (d4 <= d3), b)
    pick((d6 >= 0) & (d5 <= d6), c)
    return np.linalg.norm(closest - p, axis=1)


def fold_parts(meshes: list[Smd], *, budget: int = VERTEX_BUDGET,
               max_fraction: float = 0.15) -> tuple[Smd, FoldReport] | None:
    """All parts as ONE mesh within ``budget`` vertices (and normals), or None."""
    combined = concat_by_name(meshes)
    if combined is None:
        return None
    before = unique_vertices(combined)
    result = decimate(combined, budget, max_fraction)
    if result is None:
        return None
    folded, shift, removed = result
    if unique_normals(folded) > budget:
        return None
    return folded, FoldReport(parts=len(meshes), vertices_before=before,
                              vertices_after=unique_vertices(folded),
                              normals_after=unique_normals(folded), max_shift=shift,
                              surface_error=surface_distance(removed, folded))


__all__ = ["FoldReport", "VERTEX_BUDGET", "concat_by_name", "decimate", "fold_parts",
           "surface_distance", "unique_normals", "unique_vertices"]
