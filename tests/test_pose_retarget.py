"""Retargeting-math tests (§7.4, §10.2), run without Blender."""

from __future__ import annotations

import math

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.retarget.pose_retarget import (
    Anchor,
    compute_bases,
    world_from_bases,
)
from valve_qc_merger.transform import (
    Matrix3,
    Transform,
    axis_angle,
    euler_to_matrix,
)

# A simple arm: root -> fore -> wrist -> finger -> tip, laid along +X.
_HEADS = {
    "root": Vector3(0, 0, 0),
    "fore": Vector3(1, 0, 0),
    "wrist": Vector3(2, 0, 0),
    "finger": Vector3(3, 0, 0),
    "tip": Vector3(3.5, 0, 0),
}
_PARENT: dict[str, str | None] = {
    "root": None, "fore": "root", "wrist": "fore", "finger": "wrist", "tip": "finger",
}


def _rest(heads: dict[str, Vector3]) -> dict[str, Transform]:
    return {name: Transform(translation=head) for name, head in heads.items()}


def _identity_bases() -> dict[str, Transform]:
    return {name: Transform.identity() for name in _HEADS}


def _close(a: Vector3, b: Vector3, tol: float = 1e-9) -> bool:
    return a.distance_to(b) < tol


def test_identical_rigs_reproduce_the_source_pose() -> None:
    rest = _rest(_HEADS)
    # Author a source pose by rotating a couple of bones locally.
    given = _identity_bases()
    given["fore"] = Transform(axis_angle(Vector3(0, 0, 1), 0.4))
    given["finger"] = Transform(axis_angle(Vector3(0, 1, 0), -0.6))
    src_pose = world_from_bases(rest, _PARENT, given)

    mapping: dict[str, str | None] = {n: n for n in _HEADS}
    bases = compute_bases(rest, _PARENT, mapping, rest, src_pose, anchors=[])
    posed = world_from_bases(rest, _PARENT, bases)
    for name in _HEADS:
        assert _close(posed[name].translation, src_pose[name].translation, 1e-8), name


def test_rotation_only_preserves_target_bone_lengths() -> None:
    # The target forearm is longer than the source's; retargeting must not scale it.
    src_rest = _rest(_HEADS)
    tgt_heads = dict(_HEADS)
    tgt_heads["wrist"] = Vector3(2.7, 0, 0)  # longer fore bone
    tgt_heads["finger"] = Vector3(3.7, 0, 0)
    tgt_heads["tip"] = Vector3(4.2, 0, 0)
    tgt_rest = _rest(tgt_heads)

    given = _identity_bases()
    given["fore"] = Transform(axis_angle(Vector3(0, 0, 1), 0.5))
    src_pose = world_from_bases(src_rest, _PARENT, given)

    mapping: dict[str, str | None] = {n: n for n in _HEADS}
    bases = compute_bases(tgt_rest, _PARENT, mapping, src_rest, src_pose, anchors=[])
    posed = world_from_bases(tgt_rest, _PARENT, bases)
    # Every bone segment keeps its rest length (rotation-only, no scale).
    for child, parent in _PARENT.items():
        if parent is None:
            continue
        rest_len = tgt_heads[child].distance_to(tgt_heads[parent])
        posed_len = posed[child].translation.distance_to(posed[parent].translation)
        assert math.isclose(posed_len, rest_len, rel_tol=1e-9), child


def test_anchor_lands_the_wrist_on_the_source_wrist() -> None:
    src_rest = _rest(_HEADS)
    # Target arm sits somewhere else entirely; the anchor must pull the wrist over.
    tgt_heads = {n: Vector3(h.x, h.y + 10.0, h.z) for n, h in _HEADS.items()}
    tgt_rest = _rest(tgt_heads)

    given = _identity_bases()
    given["fore"] = Transform(axis_angle(Vector3(0, 0, 1), 0.3))
    src_pose = world_from_bases(src_rest, _PARENT, given)

    mapping: dict[str, str | None] = {n: n for n in _HEADS}
    anchor = Anchor(bone="fore", wrist="wrist", source_wrist="wrist")
    bases = compute_bases(tgt_rest, _PARENT, mapping, src_rest, src_pose, anchors=[anchor])
    posed = world_from_bases(tgt_rest, _PARENT, bases)
    assert _close(posed["wrist"].translation, src_pose["wrist"].translation, 1e-8)


def _mat_close(a: Matrix3, b: Matrix3, tol: float = 1e-8) -> bool:
    return all(abs(a[i][j] - b[i][j]) < tol for i in range(3) for j in range(3))


def test_anatomical_frame_gives_absolute_orientation_regardless_of_target_rest() -> None:
    # Target rest is rotated 90 deg about Z (a stand-in for the flat T-pose whose
    # bind orientation is unrelated to the grip). With anatomical frames set to each
    # rig's rest orientation, the correction is identity, so the target adopts the
    # source's ABSOLUTE world orientation -- not its own rest.
    rz90 = axis_angle(Vector3(0, 0, 1), math.pi / 2)
    src_rest = {name: Transform(translation=head) for name, head in _HEADS.items()}
    tgt_rest = {name: Transform(rz90, head) for name, head in _HEADS.items()}

    given = _identity_bases()
    given["fore"] = Transform(axis_angle(Vector3(0, 0, 1), 0.4))
    given["wrist"] = Transform(axis_angle(Vector3(0, 1, 0), 0.5))
    src_pose = world_from_bases(src_rest, _PARENT, given)

    mapping: dict[str, str | None] = {n: n for n in _HEADS}
    orient = {n: (src_rest[n].rotation, tgt_rest[n].rotation) for n in _HEADS}
    bases = compute_bases(tgt_rest, _PARENT, mapping, src_rest, src_pose, [], orient=orient)
    posed = world_from_bases(tgt_rest, _PARENT, bases)
    for name in ("fore", "wrist", "finger"):
        assert _mat_close(posed[name].rotation, src_pose[name].rotation), name


def test_held_tip_follows_its_parent() -> None:
    rest = _rest(_HEADS)
    given = _identity_bases()
    given["finger"] = Transform(euler_to_matrix(Vector3(0.0, 0.0, 0.7)))
    src_pose = world_from_bases(rest, _PARENT, given)

    # 'tip' has no source: it must keep an identity basis and follow 'finger'.
    mapping: dict[str, str | None] = {n: n for n in _HEADS}
    mapping["tip"] = None
    bases = compute_bases(rest, _PARENT, mapping, rest, src_pose, anchors=[])
    assert _close(bases["tip"].translation, Vector3(0, 0, 0))
    # tip world orientation equals finger world orientation composed with its rest local.
    posed = world_from_bases(rest, _PARENT, bases)
    # The tip should ride the finger's rotation: its head moves off the X axis.
    assert posed["tip"].translation.y > 1e-3
