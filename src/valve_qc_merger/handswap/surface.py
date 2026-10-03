"""Closest points on a triangle mesh (numpy, no SciPy): the hand-surface
metric of the benchmark and the palm fit share it."""

from __future__ import annotations

import numpy as np


def closest_points(points: np.ndarray, tris: np.ndarray, chunk: int = 48
                   ) -> tuple[np.ndarray, np.ndarray]:
    """For each point (P,3): the nearest point on any triangle (T,3,3) and
    its distance — Ericson's closest-point regions, vectorised over points x
    triangles."""
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    ab, ac = b - a, c - a
    nearest = np.empty((len(points), 3))
    dist = np.empty(len(points))
    for start in range(0, len(points), chunk):
        p = points[start:start + chunk, None, :]  # (C,1,3)
        ap, bp, cp = p - a, p - b, p - c
        d1, d2 = np.einsum("tk,ctk->ct", ab, ap), np.einsum("tk,ctk->ct", ac, ap)
        d3, d4 = np.einsum("tk,ctk->ct", ab, bp), np.einsum("tk,ctk->ct", ac, bp)
        d5, d6 = np.einsum("tk,ctk->ct", ab, cp), np.einsum("tk,ctk->ct", ac, cp)
        va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
        with np.errstate(divide="ignore", invalid="ignore"):
            denom = va + vb + vc
            v = np.where(denom != 0, vb / denom, 0.0)
            w = np.where(denom != 0, vc / denom, 0.0)
            closest = a + v[..., None] * ab + w[..., None] * ac  # interior
            t_ab = np.clip(d1 / np.where(d1 - d3 != 0, d1 - d3, 1), 0, 1)
            t_ac = np.clip(d2 / np.where(d2 - d6 != 0, d2 - d6, 1), 0, 1)
            t_bc = np.clip((d4 - d3) / np.where((d4 - d3) + (d5 - d6) != 0,
                                                (d4 - d3) + (d5 - d6), 1), 0, 1)
        on_ab = a + t_ab[..., None] * ab
        on_ac = a + t_ac[..., None] * ac
        on_bc = b + t_bc[..., None] * (c - b)
        closest = np.where(((vc <= 0) & (d1 >= 0) & (d3 <= 0))[..., None], on_ab, closest)
        closest = np.where(((vb <= 0) & (d2 >= 0) & (d6 <= 0))[..., None], on_ac, closest)
        closest = np.where(((va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0))[..., None],
                           on_bc, closest)
        closest = np.where(((d1 <= 0) & (d2 <= 0))[..., None], a, closest)
        closest = np.where(((d3 >= 0) & (d4 <= d3))[..., None], b, closest)
        closest = np.where(((d6 >= 0) & (d5 <= d6))[..., None], c, closest)
        d = np.linalg.norm(p - closest, axis=2)  # (C,T)
        best = d.argmin(axis=1)
        rows = np.arange(len(best))
        nearest[start:start + chunk] = closest[rows, best]
        dist[start:start + chunk] = d[rows, best]
    return nearest, dist


def points_to_mesh(points: np.ndarray, tris: np.ndarray, chunk: int = 256) -> np.ndarray:
    """Distance from each point to the nearest triangle — the same regions
    as :func:`closest_points`, but only scalars: every dot product comes from
    two matrix products and the squared distance from the barycentric (v, w)
    of the closest point, so no (points x triangles x 3) array is built
    (~10x faster; the finger fit calls this thousands of times)."""
    a = tris[:, 0]
    ab, ac = tris[:, 1] - a, tris[:, 2] - a
    abab, acac, abac = (ab * ab).sum(1), (ac * ac).sum(1), (ab * ac).sum(1)
    a_ab, a_ac, a_a = (a * ab).sum(1), (a * ac).sum(1), (a * a).sum(1)
    out = np.empty(len(points))
    for start in range(0, len(points), chunk):
        p = points[start:start + chunk]
        d1 = p @ ab.T - a_ab  # ap.ab
        d2 = p @ ac.T - a_ac  # ap.ac
        apap = (p * p).sum(1)[:, None] - 2.0 * (p @ a.T) + a_a
        d3, d4 = d1 - abab, d2 - abac  # bp.ab, bp.ac
        d5, d6 = d1 - abac, d2 - acac  # cp.ab, cp.ac
        va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
        with np.errstate(divide="ignore", invalid="ignore"):
            denom = va + vb + vc
            v = np.where(denom != 0, vb / denom, 0.0)
            w = np.where(denom != 0, vc / denom, 0.0)
            t_ab = np.clip(d1 / np.where(d1 - d3 != 0, d1 - d3, 1), 0, 1)
            t_ac = np.clip(d2 / np.where(d2 - d6 != 0, d2 - d6, 1), 0, 1)
            t_bc = np.clip((d4 - d3) / np.where((d4 - d3) + (d5 - d6) != 0,
                                                (d4 - d3) + (d5 - d6), 1), 0, 1)
        on = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        v, w = np.where(on, t_ab, v), np.where(on, 0.0, w)
        on = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        v, w = np.where(on, 0.0, v), np.where(on, t_ac, w)
        on = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
        v, w = np.where(on, 1.0 - t_bc, v), np.where(on, t_bc, w)
        on = (d1 <= 0) & (d2 <= 0)
        v, w = np.where(on, 0.0, v), np.where(on, 0.0, w)
        on = (d3 >= 0) & (d4 <= d3)
        v, w = np.where(on, 1.0, v), np.where(on, 0.0, w)
        on = (d6 >= 0) & (d5 <= d6)
        v, w = np.where(on, 0.0, v), np.where(on, 1.0, w)
        dist2 = (apap - 2.0 * v * d1 - 2.0 * w * d2 + v * v * abab
                 + 2.0 * v * w * abac + w * w * acac)
        out[start:start + chunk] = np.sqrt(np.maximum(dist2, 0.0)).min(axis=1)
    return out


def kabsch(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Rigid 4x4 moving ``src`` (N,3) onto ``dst`` (N,3) in least squares."""
    cs, cd = src.mean(axis=0), dst.mean(axis=0)
    h = (src - cs).T @ (dst - cd)
    u, _s, vt = np.linalg.svd(h)
    d = np.sign(np.linalg.det(vt.T @ u.T))
    r = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    m = np.eye(4)
    m[:3, :3], m[:3, 3] = r, cd - r @ cs
    return m


__all__ = ["closest_points", "kabsch", "points_to_mesh"]
