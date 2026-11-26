"""Hand against the WEAPON: how deep a point sits behind the weapon's skin.

The finger fit and the palm seat only ever looked at the ORIGINAL hand — its
joints, its tips, its skin. Our hand is thicker and longer, so landing its
joints where the original's were puts its skin deeper into the weapon than
the original's was; and where the original (low-poly) hand itself cut into
the weapon, the fit copied that. This module gives the retarget the missing
signal: a signed distance to the kept weapon geometry at the setup grip.

A GoldSource weapon shell is open and low-poly (v_awp: 94 boundary edges of
1202), so "inside" is not an enclosure test: a point is inside when it lies
BEHIND its nearest face (the face's outward side taken from the mesh's own
vertex normals) and mostly behind rather than beside it. On a closed mesh
(v_c4) this agrees with ray parity on 99.3 % of the samples near the surface.
"""

from __future__ import annotations

import numpy as np

# a point is behind a face only if at least this much of its distance to the
# face is along the inward normal (else it is off to the side of an edge)
BEHIND_FRACTION = 0.5


def closest(points: np.ndarray, tris: np.ndarray, chunk: int = 96
            ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Nearest point on any triangle (T,3,3) for each point (P,3): the point,
    its distance and the triangle's index (Ericson's regions, like
    surface.closest_points, which does not report the triangle)."""
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    ab, ac = b - a, c - a
    count = len(points)
    nearest = np.empty((count, 3))
    dist = np.empty(count)
    index = np.empty(count, dtype=int)
    for start in range(0, count, chunk):
        p = points[start:start + chunk, None, :]
        ap, bp, cp = p - a, p - b, p - c
        d1, d2 = np.einsum("tk,ctk->ct", ab, ap), np.einsum("tk,ctk->ct", ac, ap)
        d3, d4 = np.einsum("tk,ctk->ct", ab, bp), np.einsum("tk,ctk->ct", ac, bp)
        d5, d6 = np.einsum("tk,ctk->ct", ab, cp), np.einsum("tk,ctk->ct", ac, cp)
        va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
        with np.errstate(divide="ignore", invalid="ignore"):
            denom = va + vb + vc
            v = np.where(denom != 0, vb / denom, 0.0)
            w = np.where(denom != 0, vc / denom, 0.0)
            near = a + v[..., None] * ab + w[..., None] * ac
            t_ab = np.clip(d1 / np.where(d1 - d3 != 0, d1 - d3, 1), 0, 1)
            t_ac = np.clip(d2 / np.where(d2 - d6 != 0, d2 - d6, 1), 0, 1)
            t_bc = np.clip((d4 - d3) / np.where((d4 - d3) + (d5 - d6) != 0,
                                                (d4 - d3) + (d5 - d6), 1), 0, 1)
        near = np.where(((vc <= 0) & (d1 >= 0) & (d3 <= 0))[..., None],
                        a + t_ab[..., None] * ab, near)
        near = np.where(((vb <= 0) & (d2 >= 0) & (d6 <= 0))[..., None],
                        a + t_ac[..., None] * ac, near)
        near = np.where(((va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0))[..., None],
                        b + t_bc[..., None] * (c - b), near)
        near = np.where(((d1 <= 0) & (d2 <= 0))[..., None], a, near)
        near = np.where(((d3 >= 0) & (d4 <= d3))[..., None], b, near)
        near = np.where(((d6 >= 0) & (d5 <= d6))[..., None], c, near)
        d = np.linalg.norm(p - near, axis=2)
        best = d.argmin(axis=1)
        rows = np.arange(len(best))
        nearest[start:start + chunk] = near[rows, best]
        dist[start:start + chunk] = d[rows, best]
        index[start:start + chunk] = best
    return nearest, dist, index


def surface_samples(tris: np.ndarray) -> np.ndarray:
    """Corners, edge midpoints and centroids of (T,3,3) triangles, unique:
    a low-poly hand's edge crosses the weapon while both its ends are out."""
    parts = [tris.reshape(-1, 3), tris.mean(axis=1)]
    parts += [(tris[:, i] + tris[:, j]) / 2.0 for i, j in ((0, 1), (1, 2), (2, 0))]
    return np.unique(np.round(np.concatenate(parts), 5), axis=0)


class Shell:
    """The weapon's triangles with their outward normals."""

    def __init__(self, tris: np.ndarray, outward: np.ndarray) -> None:
        self.tris = np.asarray(tris, dtype=float).reshape(-1, 3, 3)
        self.outward = np.asarray(outward, dtype=float).reshape(-1, 3)
        self._lo = self.tris.min(axis=1) if len(self.tris) else np.zeros((0, 3))
        self._hi = self.tris.max(axis=1) if len(self.tris) else np.zeros((0, 3))

    def __len__(self) -> int:
        return len(self.tris)

    def within(self, lo: np.ndarray, hi: np.ndarray) -> Shell:
        """The triangles whose box meets the box ``lo``..``hi``."""
        keep = (self._hi >= lo).all(axis=1) & (self._lo <= hi).all(axis=1)
        return Shell(self.tris[keep], self.outward[keep])

    def depth(self, points: np.ndarray, reach: float = 2.0) -> np.ndarray:
        """How deep each point is inside the weapon (0 = outside, or farther
        than ``reach`` from any face)."""
        points = np.asarray(points, dtype=float).reshape(-1, 3)
        out = np.zeros(len(points))
        if not len(points) or not len(self.tris):
            return out
        local = self.within(points.min(axis=0) - reach, points.max(axis=0) + reach)
        if not len(local):
            return out
        near, dist, index = closest(points, local.tris)
        behind = -np.einsum("pk,pk->p", points - near, local.outward[index])
        inside = (dist <= reach) & (behind > 1e-4) & (behind >= BEHIND_FRACTION * dist)
        out[inside] = dist[inside]
        return out


def signed_distance(shell: Shell, points: np.ndarray, reach: float = 3.0) -> np.ndarray:
    """Distance from each point to the weapon's skin: positive outside (a gap),
    negative inside (that deep); ``reach`` where nothing is nearer."""
    points = np.asarray(points, dtype=float).reshape(-1, 3)
    out = np.full(len(points), reach)
    if not len(points) or not len(shell.tris):
        return out
    local = shell.within(points.min(axis=0) - reach, points.max(axis=0) + reach)
    if not len(local):
        return out
    near, dist, index = closest(points, local.tris)
    behind = -np.einsum("pk,pk->p", points - near, local.outward[index])
    inside = (behind > 1e-4) & (behind >= BEHIND_FRACTION * dist)
    return np.minimum(np.where(inside, -dist, dist), reach)


def outward_normals(tris: np.ndarray, vertex_normals: np.ndarray) -> np.ndarray:
    """Unit face normals of (T,3,3) triangles on the side their (T,3,3)
    vertex normals point to."""
    normal = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    length = np.linalg.norm(normal, axis=1, keepdims=True)
    normal = normal / np.where(length == 0, 1.0, length)
    normal[np.einsum("tk,tk->t", normal, vertex_normals.sum(axis=1)) < 0] *= -1
    return normal


__all__ = ["BEHIND_FRACTION", "Shell", "closest", "outward_normals", "signed_distance",
           "surface_samples"]
