"""Rigid transforms and the GoldSource Euler-angle convention.

SMD skeletons store each bone as a position plus three Euler angles (radians).
The Half-Life SDK (``mathlib``) turns those angles into a rotation with the
order ``R = Rz(rz) . Ry(ry) . Rx(rx)`` -- i.e. intrinsic X, then Y, then Z --
and a bone's local placement is ``p' = R . p + position``.

:class:`Transform` wraps that rigid ``(rotation, translation)`` pair with the
composition, inversion and Euler round-tripping the kinematics and retargeting
code needs. Everything is plain Python so the package stays dependency-free.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from valve_qc_merger.models.geometry import Vector3

# A 3x3 rotation matrix as rows.
Matrix3 = tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]

_IDENTITY3: Matrix3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def mat3_multiply(a: Matrix3, b: Matrix3) -> Matrix3:
    """Multiply two 3x3 matrices (``a . b``)."""
    return tuple(  # type: ignore[return-value]
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3)
    )


def mat3_transpose(m: Matrix3) -> Matrix3:
    """Transpose a 3x3 matrix (the inverse of a rotation matrix)."""
    return (
        (m[0][0], m[1][0], m[2][0]),
        (m[0][1], m[1][1], m[2][1]),
        (m[0][2], m[1][2], m[2][2]),
    )


def _apply3(m: Matrix3, v: Vector3) -> Vector3:
    return Vector3(
        m[0][0] * v.x + m[0][1] * v.y + m[0][2] * v.z,
        m[1][0] * v.x + m[1][1] * v.y + m[1][2] * v.z,
        m[2][0] * v.x + m[2][1] * v.y + m[2][2] * v.z,
    )


def _normalize(v: Vector3) -> Vector3:
    length = math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)
    if length == 0.0:
        return v
    return Vector3(v.x / length, v.y / length, v.z / length)


def rotation_angle(m: Matrix3) -> float:
    """The rotation angle (radians) of a rotation matrix."""
    trace = m[0][0] + m[1][1] + m[2][2]
    return math.acos(max(-1.0, min(1.0, (trace - 1.0) / 2.0)))


def clamp_rotation(m: Matrix3, max_angle: float) -> Matrix3:
    """Scale a rotation down so its angle does not exceed ``max_angle`` radians."""
    angle = rotation_angle(m)
    if angle <= max_angle or angle < 1e-6:
        return m
    axis = _normalize(Vector3(m[2][1] - m[1][2], m[0][2] - m[2][0], m[1][0] - m[0][1]))
    s = math.sin(max_angle)
    c = math.cos(max_angle)
    x, y, z = axis.x, axis.y, axis.z
    t = 1.0 - c
    return (
        (t * x * x + c, t * x * y - s * z, t * x * z + s * y),
        (t * x * y + s * z, t * y * y + c, t * y * z - s * x),
        (t * x * z - s * y, t * y * z + s * x, t * z * z + c),
    )


def axis_angle(axis: Vector3, angle: float) -> Matrix3:
    """Rotation matrix of ``angle`` radians about ``axis`` (Rodrigues formula)."""
    unit = _normalize(axis)
    x, y, z = unit.x, unit.y, unit.z
    s = math.sin(angle)
    c = math.cos(angle)
    t = 1.0 - c
    return (
        (t * x * x + c, t * x * y - s * z, t * x * z + s * y),
        (t * x * y + s * z, t * y * y + c, t * y * z - s * x),
        (t * x * z - s * y, t * y * z + s * x, t * z * z + c),
    )


def rotation_between(a: Vector3, b: Vector3) -> Matrix3:
    """Minimal rotation matrix taking direction ``a`` onto direction ``b``."""
    u = _normalize(a)
    w = _normalize(b)
    cross = Vector3(
        u.y * w.z - u.z * w.y,
        u.z * w.x - u.x * w.z,
        u.x * w.y - u.y * w.x,
    )
    dot = u.x * w.x + u.y * w.y + u.z * w.z
    if dot > 0.9999999:
        return _IDENTITY3
    if dot < -0.9999999:
        # Opposed: rotate 180 degrees about any axis perpendicular to u.
        axis = Vector3(1.0, 0.0, 0.0)
        if abs(u.x) > 0.9:
            axis = Vector3(0.0, 1.0, 0.0)
        perp = _normalize(
            Vector3(
                u.y * axis.z - u.z * axis.y,
                u.z * axis.x - u.x * axis.z,
                u.x * axis.y - u.y * axis.x,
            )
        )
        x, y, z = perp.x, perp.y, perp.z
        return (
            (2 * x * x - 1, 2 * x * y, 2 * x * z),
            (2 * x * y, 2 * y * y - 1, 2 * y * z),
            (2 * x * z, 2 * y * z, 2 * z * z - 1),
        )
    vx, vy, vz = cross.x, cross.y, cross.z
    k = 1.0 / (1.0 + dot)
    # R = I + [v]_x + [v]_x^2 * k  (Rodrigues for the minimal arc).
    return (
        (1 - (vy * vy + vz * vz) * k, -vz + vx * vy * k, vy + vx * vz * k),
        (vz + vx * vy * k, 1 - (vx * vx + vz * vz) * k, -vx + vy * vz * k),
        (-vy + vx * vz * k, vx + vy * vz * k, 1 - (vx * vx + vy * vy) * k),
    )


def euler_to_matrix(euler: Vector3) -> Matrix3:
    """Build a rotation matrix from GoldSource Euler angles (radians).

    Convention: ``R = Rz(z) . Ry(y) . Rx(x)`` (intrinsic X, then Y, then Z),
    matching the Half-Life SDK's ``AngleQuaternion``.
    """
    sx, cx = math.sin(euler.x), math.cos(euler.x)
    sy, cy = math.sin(euler.y), math.cos(euler.y)
    sz, cz = math.sin(euler.z), math.cos(euler.z)
    return (
        (cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx),
        (sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx),
        (-sy, cy * sx, cy * cx),
    )


def matrix_to_euler(m: Matrix3) -> Vector3:
    """Recover GoldSource Euler angles from a rotation matrix.

    Inverse of :func:`euler_to_matrix`. Falls back gracefully at the
    gimbal-lock poles (``|sy| == 1``).
    """
    sy = -m[2][0]
    sy = max(-1.0, min(1.0, sy))
    y = math.asin(sy)
    if abs(m[2][0]) < 0.9999999:
        x = math.atan2(m[2][1], m[2][2])
        z = math.atan2(m[1][0], m[0][0])
    else:
        # Gimbal lock: pitch at +/-90 deg; fold roll into yaw.
        x = math.atan2(-m[1][2], m[1][1])
        z = 0.0
    return Vector3(x, y, z)


@dataclass(frozen=True, slots=True)
class Transform:
    """A rigid transform: a 3x3 rotation followed by a translation."""

    rotation: Matrix3 = _IDENTITY3
    translation: Vector3 = Vector3(0.0, 0.0, 0.0)

    @classmethod
    def identity(cls) -> Transform:
        """The identity transform."""
        return cls()

    @classmethod
    def from_pos_euler(cls, position: Vector3, euler: Vector3) -> Transform:
        """Build a bone-local transform from an SMD ``position``/``rotation`` pair."""
        return cls(euler_to_matrix(euler), position)

    def to_euler(self) -> Vector3:
        """Return this transform's rotation as GoldSource Euler angles."""
        return matrix_to_euler(self.rotation)

    def transform_point(self, point: Vector3) -> Vector3:
        """Apply the transform to a point: ``R . point + translation``."""
        rotated = _apply3(self.rotation, point)
        return Vector3(
            rotated.x + self.translation.x,
            rotated.y + self.translation.y,
            rotated.z + self.translation.z,
        )

    def rotate_vector(self, vector: Vector3) -> Vector3:
        """Apply only the rotation (for directions such as normals)."""
        return _apply3(self.rotation, vector)

    def compose(self, other: Transform) -> Transform:
        """Return ``self . other`` (apply ``other`` first, then ``self``)."""
        rotation = mat3_multiply(self.rotation, other.rotation)
        translation = self.transform_point(other.translation)
        return Transform(rotation, translation)

    def inverse(self) -> Transform:
        """Return the inverse transform."""
        inv_rot = mat3_transpose(self.rotation)
        inv_trans = _apply3(inv_rot, self.translation)
        return Transform(inv_rot, Vector3(-inv_trans.x, -inv_trans.y, -inv_trans.z))


__all__ = [
    "Matrix3",
    "Transform",
    "axis_angle",
    "clamp_rotation",
    "euler_to_matrix",
    "mat3_multiply",
    "mat3_transpose",
    "matrix_to_euler",
    "rotation_angle",
    "rotation_between",
]
