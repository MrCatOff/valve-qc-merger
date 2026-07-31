"""Finger-curl hinge-IK tests (§7.6), run without Blender."""

from __future__ import annotations

import math

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.retarget.grip_ik import (
    calibrate_axis_sign,
    rest_locals,
    solve_finger,
    tip_error,
)
from valve_qc_merger.transform import Transform

# A straight finger: base at origin, three unit joints along +Y, then a tip.
_HEADS = {"j0": (0, 0, 0), "j1": (0, 1, 0), "j2": (0, 2, 0), "tip": (0, 3, 0)}
_CHAIN = ["j0", "j1", "j2", "tip"]
_DOF = ["j0", "j1", "j2"]
_PARENT: dict[str, str | None] = {"j0": "wrist", "j1": "j0", "j2": "j1", "tip": "j2"}
_REST = {name: Transform(translation=Vector3(*h)) for name, h in _HEADS.items()}
_REST["wrist"] = Transform()  # base parent at the origin
_LOCAL = rest_locals(_CHAIN, _PARENT, _REST)
_AXIS = Vector3(0.0, 0.0, 1.0)  # curl in the XY plane, toward +X
_WIDE = {b: (-math.pi, math.pi) for b in _DOF}


def test_reaches_a_target_within_range() -> None:
    target = Vector3(1.5, 1.5, 0.0)  # comfortably inside the 3-unit reach
    basis, _ = solve_finger(
        _CHAIN, _DOF, Transform(), _LOCAL, target, _WIDE, axis=_AXIS, iterations=30,
    )
    assert tip_error(_CHAIN, Transform(), _LOCAL, basis, target) < 0.05


def test_curl_is_planar() -> None:
    # A hinge about Z can never move the finger out of the XY plane, no matter how
    # unreachable the target is — the structural guarantee of anatomical curls.
    target = Vector3(1.0, -1.0, 2.0)  # off-plane target
    basis, _ = solve_finger(
        _CHAIN, _DOF, Transform(), _LOCAL, target, _WIDE, axis=_AXIS, iterations=30,
    )
    from valve_qc_merger.retarget.grip_ik import _fk  # test-only peek

    posed = _fk(_CHAIN, Transform(), _LOCAL, basis)
    for bone in _CHAIN:
        assert abs(posed[bone].translation.z) < 1e-9


def test_hinge_limits_are_respected() -> None:
    # Flexion capped at 30 degrees per joint: the far target is unreachable, but no
    # joint may exceed the cap and none may bend backward.
    cap = math.radians(30)
    limits = {b: (0.0, cap) for b in _DOF}
    target = Vector3(3.0, 0.0, 0.0)
    _, angles = solve_finger(
        _CHAIN, _DOF, Transform(), _LOCAL, target, limits, axis=_AXIS, iterations=30,
    )
    for bone in _DOF:
        assert -1e-9 <= angles[bone] <= cap + 1e-9


def test_axis_sign_calibration_picks_the_curl_toward_the_target() -> None:
    # Toward +X the correct hinge is -Z (right-hand rule: -Z rotates +Y toward +X).
    target = Vector3(2.0, 0.5, 0.0)
    sign = calibrate_axis_sign(_CHAIN, _DOF, Transform(), _LOCAL, target, _AXIS)
    assert sign == -1.0
    flipped = Vector3(0.0, 0.0, -1.0)
    assert calibrate_axis_sign(_CHAIN, _DOF, Transform(), _LOCAL, target, flipped) == 1.0


def test_pre_basis_re_aims_the_chain_before_the_curl() -> None:
    # An abduction aim of -90 deg about Z at the base re-points the whole straight
    # finger from +Y to +X before any hinge curl is applied.
    from valve_qc_merger.transform import axis_angle

    pre = {"j0": Transform(axis_angle(Vector3(0, 0, 1), -math.pi / 2))}
    basis, _ = solve_finger(
        _CHAIN, _DOF, Transform(), _LOCAL, Vector3(3.0, 0.0, 0.0), _WIDE,
        axis=_AXIS, pre_basis=pre, iterations=0,
    )
    assert tip_error(_CHAIN, Transform(), _LOCAL, basis, Vector3(3.0, 0.0, 0.0)) < 1e-6


def test_max_step_clamps_per_frame_travel() -> None:
    # The previous frame held the finger straight; the new target demands a big
    # curl. With max_step the joints may travel at most that far in one frame.
    step = math.radians(20)
    target = Vector3(2.0, 0.5, 0.0)
    axis = Vector3(0.0, 0.0, -1.0)
    warm = {b: 0.0 for b in _DOF}
    _, angles = solve_finger(
        _CHAIN, _DOF, Transform(), _LOCAL, target, _WIDE, axis=axis,
        warm_start=warm, max_step=step, iterations=30,
    )
    for bone in _DOF:
        assert abs(angles[bone]) <= step + 1e-9
    assert any(abs(angles[bone]) > 1e-3 for bone in _DOF)  # it did move


def test_warm_start_is_accepted_and_stays_close() -> None:
    target = Vector3(1.2, 1.8, 0.0)
    axis = Vector3(0.0, 0.0, -1.0)
    first_basis, first_angles = solve_finger(
        _CHAIN, _DOF, Transform(), _LOCAL, target, _WIDE, axis=axis, iterations=30,
    )
    second_basis, second_angles = solve_finger(
        _CHAIN, _DOF, Transform(), _LOCAL, target, _WIDE, axis=axis,
        warm_start=first_angles, iterations=30,
    )
    assert tip_error(_CHAIN, Transform(), _LOCAL, second_basis, target) < 0.05
    for bone in _DOF:
        assert abs(second_angles[bone] - first_angles[bone]) < 0.2
