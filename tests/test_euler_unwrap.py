"""Euler continuity unwrap tests (§7.8), no Blender."""

from __future__ import annotations

import math

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd
from valve_qc_merger.retarget.euler_unwrap import unwrap_smd
from valve_qc_merger.transform import (
    euler_to_matrix,
    mat3_multiply,
    mat3_transpose,
    rotation_angle,
)


def _anim(rotations: list[Vector3]) -> Smd:
    frames = [
        Frame(t, (BonePose(0, Vector3(0.0, 0.0, 0.0), rot),))
        for t, rot in enumerate(rotations)
    ]
    return Smd(nodes=[Node(0, "b", -1)], frames=frames)


def _max_component_jump(smd: Smd) -> float:
    prev: Vector3 | None = None
    worst = 0.0
    for frame in smd.frames:
        rot = frame.poses[0].rotation
        if prev is not None:
            worst = max(worst, abs(rot.x - prev.x), abs(rot.y - prev.y), abs(rot.z - prev.z))
        prev = rot
    return worst


def test_unwrap_removes_2pi_wrap() -> None:
    # Same small rotation, but the second frame is named +2pi away on Z.
    smd = _anim([Vector3(0.0, 0.0, 3.0), Vector3(0.0, 0.0, 3.0 - 2 * math.pi)])
    out, changed = unwrap_smd(smd)
    assert changed == 1
    assert _max_component_jump(out) < 1e-6


def test_unwrap_resolves_gimbal_flip_without_changing_rotation() -> None:
    original = Vector3(math.radians(82), math.radians(149), math.radians(27))
    flip = Vector3(original.x + math.pi, math.pi - original.y, original.z + math.pi)
    smd = _anim([original, flip])  # BST could emit the flipped naming next frame
    out, changed = unwrap_smd(smd)
    assert changed == 1
    # Continuous naming...
    assert _max_component_jump(out) < math.radians(5)
    # ...and the rotation itself is untouched on every frame.
    for before, after in zip(smd.frames, out.frames, strict=True):
        delta = rotation_angle(mat3_multiply(
            euler_to_matrix(after.poses[0].rotation),
            mat3_transpose(euler_to_matrix(before.poses[0].rotation)),
        ))
        assert math.degrees(delta) < 1e-3


def test_unwrap_leaves_continuous_track_untouched() -> None:
    smd = _anim([Vector3(0.0, 0.0, 0.1), Vector3(0.0, 0.0, 0.2), Vector3(0.0, 0.0, 0.3)])
    out, changed = unwrap_smd(smd)
    assert changed == 0
