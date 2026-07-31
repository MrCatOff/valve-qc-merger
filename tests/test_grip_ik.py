"""Finger-curl IK tests (§7.6), run without Blender."""

from __future__ import annotations

import math

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.retarget.grip_ik import Limit, rest_locals, solve_finger, tip_error
from valve_qc_merger.transform import Transform, matrix_to_euler

# A straight finger: base at origin, three unit joints along +Y, then a tip.
_HEADS = {"j0": (0, 0, 0), "j1": (0, 1, 0), "j2": (0, 2, 0), "tip": (0, 3, 0)}
_CHAIN = ["j0", "j1", "j2", "tip"]
_DOF = ["j0", "j1", "j2"]
_PARENT: dict[str, str | None] = {"j0": "wrist", "j1": "j0", "j2": "j1", "tip": "j2"}
_REST = {name: Transform(translation=Vector3(*h)) for name, h in _HEADS.items()}
_REST["wrist"] = Transform()  # base parent at the origin
_LOCAL = rest_locals(_CHAIN, _PARENT, _REST)
_WIDE = Limit((-math.pi, -math.pi, -math.pi), (math.pi, math.pi, math.pi))


def test_reaches_a_target_within_range() -> None:
    target = Vector3(1.5, 1.5, 0.0)  # comfortably inside the 3-unit reach
    basis = solve_finger(
        _CHAIN, _DOF, Transform(), _LOCAL, target,
        {b: _WIDE for b in _DOF}, iterations=30,
    )
    assert tip_error(_CHAIN, Transform(), _LOCAL, basis, target) < 0.05


def test_curl_reduces_tip_distance() -> None:
    # A target the straight finger misses badly; solving must move the tip closer.
    target = Vector3(2.0, 0.5, 0.0)
    straight = {b: Transform.identity() for b in _CHAIN}
    before = tip_error(_CHAIN, Transform(), _LOCAL, straight, target)
    basis = solve_finger(
        _CHAIN, _DOF, Transform(), _LOCAL, target, {b: _WIDE for b in _DOF}, iterations=30,
    )
    after = tip_error(_CHAIN, Transform(), _LOCAL, basis, target)
    assert after < before * 0.25


def test_joint_limits_are_respected() -> None:
    # Flexion only about Z, capped at 30 degrees per joint; the tip cannot reach a
    # far target, but no joint may exceed its limit.
    cap = math.radians(30)
    limit = Limit((0.0, 0.0, 0.0), (0.0, 0.0, cap))
    target = Vector3(3.0, 0.0, 0.0)
    basis = solve_finger(
        _CHAIN, _DOF, Transform(), _LOCAL, target, {b: limit for b in _DOF}, iterations=30,
    )
    for bone in _DOF:
        euler = matrix_to_euler(basis[bone].rotation)
        assert -1e-6 <= euler.z <= cap + 1e-6
        assert abs(euler.x) < 1e-6 and abs(euler.y) < 1e-6


def test_warm_start_is_accepted_and_stays_close() -> None:
    target = Vector3(1.2, 1.8, 0.0)
    first = solve_finger(
        _CHAIN, _DOF, Transform(), _LOCAL, target, {b: _WIDE for b in _DOF}, iterations=30,
    )
    # Re-solving the same target warm-started from the solution stays put.
    second = solve_finger(
        _CHAIN, _DOF, Transform(), _LOCAL, target, {b: _WIDE for b in _DOF},
        warm_start=first, iterations=30,
    )
    assert tip_error(_CHAIN, Transform(), _LOCAL, second, target) < 0.05
