"""Anatomical palm/finger frames shared by the CSO asset and the original
hand analysis.

Everything is computed from JOINT POSITIONS (bone heads, skinned-vertex
centroids) — never from imported SMD bone axes, which are arbitrary
(measured on the stock hands: opposite/perpendicular to the finger).
"""
from __future__ import annotations

import numpy as np

from .math3d import normalized


def hand_frame(wrist_pos, finger_root_positions, thumb_i=None,
               thumb_anchor=None, flip=False):
    """Palm frame: origin mid-palm, X = fingers-forward, Y = across the
    palm along the KNUCKLE LINE (signed toward the thumb), Z = X x Y.

    The across axis comes from the four non-thumb knuckles (almost
    colinear on both hand models) and never from a thumb vector: the stock
    Valve thumb root sits at the wrist, so a thumb-based across axis rolls
    the whole palm. thumb_anchor only picks the sign of Y.
    Returns (4x4 frame, thumb_index)."""
    w = np.asarray(wrist_pos, dtype=float)
    roots = [np.asarray(p, dtype=float) for p in finger_root_positions]

    if thumb_i is None:
        def isolation(i):
            others = [roots[j] for j in range(len(roots)) if j != i]
            oc = np.mean(others, axis=0)
            return np.linalg.norm(roots[i] - oc)
        thumb_i = max(range(len(roots)), key=isolation)

    others = [roots[j] for j in range(len(roots)) if j != thumb_i]
    c = np.mean(others, axis=0)
    fwd = normalized(c - w)

    line, best = np.zeros(3), -1.0
    for i in range(len(others)):
        for j in range(i + 1, len(others)):
            d = others[i] - others[j]
            n = np.linalg.norm(d)
            if n > best:
                best, line = n, d
    anchor = np.asarray(thumb_anchor, dtype=float) \
        if thumb_anchor is not None else roots[thumb_i]
    if np.dot(line, anchor - c) < 0:
        line = -line
    if flip:
        line = -line
    across = normalized(line - fwd * np.dot(line, fwd))
    normal = np.cross(fwd, across)
    origin = (w + c) / 2.0
    m = np.eye(4)
    m[:3, 0], m[:3, 1], m[:3, 2], m[:3, 3] = fwd, across, normal, origin
    return m, thumb_i


def bone_anat_3x3(y_dir, curl_hint) -> np.ndarray:
    """Anatomical frame of a finger bone: Y along the bone, X the curl
    axis (palm across-direction orthogonalized against Y), Z = X x Y."""
    y = normalized(y_dir)
    x = np.asarray(curl_hint, dtype=float) - y * np.dot(curl_hint, y)
    if np.linalg.norm(x) < 1e-4:
        x = np.array([1.0, 0.0, 0.0]) if abs(y[0]) < 0.9 \
            else np.array([0.0, 1.0, 0.0])
        x = x - y * np.dot(x, y)
    x = normalized(x)
    z = np.cross(x, y)
    return np.column_stack([x, y, z])
