"""Tests for the rigid-transform and Euler-angle math."""

from __future__ import annotations

import math

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.transform import (
    Transform,
    euler_to_matrix,
    matrix_to_euler,
)


def _close(a: Vector3, b: Vector3, tol: float = 1e-9) -> bool:
    return abs(a.x - b.x) < tol and abs(a.y - b.y) < tol and abs(a.z - b.z) < tol


def test_euler_matrix_round_trip() -> None:
    for euler in [
        Vector3(0.0, 0.0, 0.0),
        Vector3(0.3, -0.7, 1.1),
        Vector3(1.5, 0.2, -2.0),
        Vector3(-0.9, 0.8, 0.4),
    ]:
        recovered = matrix_to_euler(euler_to_matrix(euler))
        assert _close(matrix_to_euler(euler_to_matrix(recovered)), recovered, 1e-6)


def test_compose_matches_sequential_application() -> None:
    a = Transform.from_pos_euler(Vector3(1.0, 2.0, 3.0), Vector3(0.2, -0.5, 0.7))
    b = Transform.from_pos_euler(Vector3(-2.0, 0.5, 1.0), Vector3(-0.3, 0.9, 0.1))
    point = Vector3(0.7, -1.3, 2.2)
    composed = a.compose(b).transform_point(point)
    sequential = a.transform_point(b.transform_point(point))
    assert _close(composed, sequential, 1e-9)


def test_inverse_cancels_transform() -> None:
    t = Transform.from_pos_euler(Vector3(4.0, -1.0, 2.5), Vector3(0.6, 1.2, -0.4))
    identity = t.inverse().compose(t)
    point = Vector3(3.0, 1.0, -2.0)
    assert _close(identity.transform_point(point), point, 1e-9)


def test_rotation_places_point_as_expected() -> None:
    # 90 degrees about Z maps +X to +Y.
    t = Transform.from_pos_euler(Vector3(0.0, 0.0, 0.0), Vector3(0.0, 0.0, math.pi / 2))
    assert _close(t.transform_point(Vector3(1.0, 0.0, 0.0)), Vector3(0.0, 1.0, 0.0), 1e-9)
