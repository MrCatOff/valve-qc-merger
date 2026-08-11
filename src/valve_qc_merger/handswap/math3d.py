"""Rigid-transform math for the SMD domain (numpy, no Blender).

Conventions (match studiomdl / CLAUDE.md):
  - SMD bone keys are (px py pz rx ry rz), rotation matrix R = Rz @ Ry @ Rx.
  - World matrices: world(bone) = world(parent) @ local(bone).
  - 4x4 matrices are row-major numpy arrays; points are column-applied via
    (M[:3, :3] @ p + M[:3, 3]).
"""
from __future__ import annotations

import numpy as np


def euler_to_mat3(rx: float, ry: float, rz: float) -> np.ndarray:
    sx, cx = np.sin(rx), np.cos(rx)
    sy, cy = np.sin(ry), np.cos(ry)
    sz, cz = np.sin(rz), np.cos(rz)
    return np.array([
        [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
        [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
        [-sy,     cy * sx,                cy * cx],
    ])


def mat3_to_euler(m: np.ndarray) -> tuple[float, float, float]:
    """Inverse of euler_to_mat3 (R = Rz@Ry@Rx)."""
    sy = -m[2, 0]
    sy = min(1.0, max(-1.0, sy))
    ry = np.arcsin(sy)
    if abs(sy) < 0.9999999:
        rx = np.arctan2(m[2, 1], m[2, 2])
        rz = np.arctan2(m[1, 0], m[0, 0])
    else:  # gimbal: cy == 0, fold rz into rx
        rx = np.arctan2(-m[1, 2], m[1, 1])
        rz = 0.0
    return float(rx), float(ry), float(rz)


def compose(rot3: np.ndarray, pos) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = rot3
    m[:3, 3] = pos
    return m


def local_matrix(pos, rot) -> np.ndarray:
    return compose(euler_to_mat3(*rot), pos)


def inv_rigid(m: np.ndarray) -> np.ndarray:
    r = m[:3, :3].T
    out = np.eye(4)
    out[:3, :3] = r
    out[:3, 3] = -r @ m[:3, 3]
    return out


def apply(m: np.ndarray, p) -> np.ndarray:
    return m[:3, :3] @ np.asarray(p, dtype=float) + m[:3, 3]


def normalized(v) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    n = np.linalg.norm(v)
    if n < 1e-12:
        raise ValueError("zero-length vector")
    return v / n


def rotation_between(a, b) -> np.ndarray:
    """Shortest-arc 3x3 rotation taking direction a to direction b."""
    a, b = normalized(a), normalized(b)
    c = float(np.dot(a, b))
    axis = np.cross(a, b)
    s = np.linalg.norm(axis)
    if s < 1e-9:
        if c > 0:
            return np.eye(3)
        # 180 deg: any axis orthogonal to a
        ortho = np.cross(a, [1.0, 0.0, 0.0])
        if np.linalg.norm(ortho) < 1e-6:
            ortho = np.cross(a, [0.0, 1.0, 0.0])
        return axis_angle(normalized(ortho), np.pi)
    axis = axis / s
    return axis_angle(axis, float(np.arctan2(s, c)))


def axis_angle(axis, angle: float) -> np.ndarray:
    axis = normalized(axis)
    x, y, z = axis
    c, s = np.cos(angle), np.sin(angle)
    C = 1 - c
    return np.array([
        [c + x * x * C, x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, c + z * z * C],
    ])


def twist_angle(rot3: np.ndarray, axis) -> float:
    """Twist component of a rotation about the given axis (swing-twist)."""
    axis = normalized(axis)
    # quaternion of rot3
    w, x, y, z = mat3_to_quat(rot3)
    proj = np.array([x, y, z]).dot(axis)
    tw = np.array([w, *(proj * axis)])
    n = np.linalg.norm(tw)
    if n < 1e-12:
        return 0.0
    tw /= n
    ang = 2.0 * np.arctan2(np.linalg.norm(tw[1:]), tw[0])
    if np.dot(tw[1:], axis) < 0:
        ang = -ang
    # wrap to [-pi, pi]
    if ang > np.pi:
        ang -= 2 * np.pi
    if ang < -np.pi:
        ang += 2 * np.pi
    return float(ang)


def mat3_to_quat(m: np.ndarray) -> tuple[float, float, float, float]:
    t = np.trace(m)
    if t > 0:
        s = np.sqrt(t + 1.0) * 2
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w = (m[2, 1] - m[1, 2]) / s
        x = 0.25 * s
        y = (m[0, 1] + m[1, 0]) / s
        z = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w = (m[0, 2] - m[2, 0]) / s
        x = (m[0, 1] + m[1, 0]) / s
        y = 0.25 * s
        z = (m[1, 2] + m[2, 1]) / s
    else:
        s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w = (m[1, 0] - m[0, 1]) / s
        x = (m[0, 2] + m[2, 0]) / s
        y = (m[1, 2] + m[2, 1]) / s
        z = 0.25 * s
    return float(w), float(x), float(y), float(z)


def rotation_angle(rot3: np.ndarray) -> float:
    """Magnitude of a rotation in radians."""
    c = (np.trace(rot3) - 1.0) / 2.0
    return float(np.arccos(min(1.0, max(-1.0, c))))


def orthonormalize(rot3: np.ndarray) -> np.ndarray:
    """Nearest rotation matrix (SVD projection)."""
    u, _s, vt = np.linalg.svd(rot3)
    r = u @ vt
    if np.linalg.det(r) < 0:
        u[:, -1] *= -1
        r = u @ vt
    return r


def frame_from_axes(x, y_hint, origin=None) -> np.ndarray:
    """Right-handed frame: X exact, Y = hint orthogonalized, Z = X x Y."""
    x = normalized(x)
    y = np.asarray(y_hint, dtype=float)
    y = y - x * np.dot(y, x)
    y = normalized(y)
    z = np.cross(x, y)
    m = np.eye(4)
    m[:3, 0], m[:3, 1], m[:3, 2] = x, y, z
    if origin is not None:
        m[:3, 3] = origin
    return m
