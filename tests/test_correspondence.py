"""Synthetic-rig tests for geometric bone correspondence (§7.3, §10.1).

These run without Blender: hands are generated programmatically, transformed
arbitrarily, mirrored and placed on the same side of the origin, and the
recovered map is asserted exactly.
"""

from __future__ import annotations

import pytest

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.retarget.correspondence import (
    CorrespondenceError,
    RigBone,
    build_correspondence,
)
from valve_qc_merger.transform import Transform, axis_angle

# Canonical right-hand layout in the hand's local frame:
#   palm forward = +Y, knuckle spread = +X, palm normal = +Z.
# Four non-thumb bases sit on a line at y=1; the thumb sits low and points
# sideways (abducted) and slightly out of the palm plane (planar outlier).
_NON_THUMB = {  # name -> base (x, y, z)
    "index": (-0.45, 1.0, 0.0),
    "middle": (-0.15, 1.05, 0.0),
    "ring": (0.15, 1.0, 0.0),
    "pinky": (0.45, 0.9, 0.0),
}
_THUMB_BASE = (-0.7, 0.35, 0.18)
_THUMB_DIR = (-0.45, 0.1, 0.0)  # mostly along -X: abducted from the +Y others


def _hand(
    prefix: str,
    xform: Transform,
    *,
    chirality: float = 1.0,
    joints: int = 3,
) -> tuple[list[RigBone], dict[str, str]]:
    """Build one hand; return its bones and a role -> bone-name map."""
    bones: list[RigBone] = []
    roles: dict[str, str] = {}

    def place(local: tuple[float, float, float]) -> Vector3:
        return xform.transform_point(Vector3(chirality * local[0], local[1], local[2]))

    forearm = f"{prefix}_forearm"
    wrist = f"{prefix}_wrist"
    bones.append(RigBone(forearm, None, place((0.0, -1.0, 0.0)), place((0.0, 0.0, 0.0))))
    bones.append(RigBone(wrist, forearm, place((0.0, 0.0, 0.0)), place((0.0, 0.3, 0.0))))
    roles["forearm"] = forearm
    roles["wrist"] = wrist

    def chain(finger: str, base: tuple[float, float, float],
              direction: tuple[float, float, float], count: int) -> None:
        parent = wrist
        px, py, pz = base
        dx, dy, dz = direction
        for depth in range(count):
            name = f"{prefix}_{finger}{depth}"
            head = (px + dx * depth, py + dy * depth, pz + dz * depth)
            tail = (px + dx * (depth + 1), py + dy * (depth + 1), pz + dz * (depth + 1))
            bones.append(RigBone(name, parent, place(head), place(tail)))
            if depth == 0:
                roles[finger] = name
            parent = name

    for finger, base in _NON_THUMB.items():
        chain(finger, base, (0.0, 0.35, 0.0), joints)
    chain("thumb", _THUMB_BASE, _THUMB_DIR, joints)
    return bones, roles


def _xform(angle: float = 0.0, axis: Vector3 | None = None,
           trans: Vector3 | None = None) -> Transform:
    axis = axis if axis is not None else Vector3(0, 0, 1)
    trans = trans if trans is not None else Vector3(0, 0, 0)
    return Transform(axis_angle(axis, angle), trans)


def test_single_hand_maps_every_finger_by_geometry() -> None:
    src, src_roles = _hand("S", _xform(), joints=3)
    tgt, tgt_roles = _hand("Bip01 R", _xform(trans=Vector3(10, 0, 0)), joints=3)
    corr = build_correspondence(src, {b.name for b in src}, tgt)
    mapping = corr.as_dict()
    for role in ("thumb", "index", "middle", "ring", "pinky", "wrist"):
        assert mapping[tgt_roles[role]] == src_roles[role], role


def test_correspondence_is_invariant_to_world_transform() -> None:
    src, src_roles = _hand("S", _xform(angle=1.1, axis=Vector3(0.3, 1, 0.2)))
    tgt, tgt_roles = _hand(
        "Bip01 R", _xform(angle=-2.0, axis=Vector3(1, 0.5, 0.1), trans=Vector3(-7, 4, 9))
    )
    mapping = build_correspondence(src, {b.name for b in src}, tgt).as_dict()
    for role in ("thumb", "index", "middle", "ring", "pinky"):
        assert mapping[tgt_roles[role]] == src_roles[role], role


def test_source_shorter_chain_holds_the_target_tip() -> None:
    src, src_roles = _hand("S", _xform(), joints=2)  # base, mid
    tgt, tgt_roles = _hand("Bip01 R", _xform(trans=Vector3(5, 0, 0)), joints=3)  # + tip
    maps = {m.target: m for m in build_correspondence(src, {b.name for b in src}, tgt).maps}
    tip = "Bip01 R_thumb2"
    assert maps[tip].source is None
    assert maps[tip].role == "tip"


def _two_arms(
    tgt_left_chirality: float, src_left_chirality: float,
    *, src_left_trans: Vector3, src_right_trans: Vector3,
) -> tuple[list[RigBone], list[RigBone], dict[str, str], dict[str, str]]:
    src_l, roles_sl = _hand("SL", _xform(trans=src_left_trans), chirality=src_left_chirality)
    src_r, roles_sr = _hand("SR", _xform(trans=src_right_trans), chirality=-src_left_chirality)
    tgt_l, roles_tl = _hand(
        "Bip01 L", _xform(trans=Vector3(-3, 0, 0)), chirality=tgt_left_chirality
    )
    tgt_r, roles_tr = _hand(
        "Bip01 R", _xform(trans=Vector3(3, 0, 0)), chirality=-tgt_left_chirality
    )
    return src_l + src_r, tgt_l + tgt_r, {**{f"L_{k}": v for k, v in roles_sl.items()},
                                          **{f"R_{k}": v for k, v in roles_sr.items()}}, \
        {**{f"L_{k}": v for k, v in roles_tl.items()},
         **{f"R_{k}": v for k, v in roles_tr.items()}}


def test_two_arms_pair_by_chirality_not_world_position() -> None:
    # Both source arms sit far on +X (same side of origin); pairing must use
    # handedness, not an X sign.
    src, tgt, src_roles, tgt_roles = _two_arms(
        tgt_left_chirality=1.0, src_left_chirality=1.0,
        src_left_trans=Vector3(20, 0, 0), src_right_trans=Vector3(25, 0, 0),
    )
    mapping = build_correspondence(src, {b.name for b in src}, tgt).as_dict()
    # Target left (chirality +1) must map to the source left arm (chirality +1).
    assert mapping[tgt_roles["L_wrist"]] == src_roles["L_wrist"]
    assert mapping[tgt_roles["R_wrist"]] == src_roles["R_wrist"]
    assert mapping[tgt_roles["L_thumb"]] == src_roles["L_thumb"]


def test_arm_count_mismatch_raises() -> None:
    one, _ = _hand("S", _xform())
    tgt_r, _ = _hand("Bip01 R", _xform(trans=Vector3(9, 0, 0)))
    tgt_l, _ = _hand("Bip01 L", _xform(trans=Vector3(-9, 0, 0)))
    with pytest.raises(CorrespondenceError, match="arm count mismatch"):
        build_correspondence(one, {b.name for b in one}, tgt_r + tgt_l)


def test_force_pairing_overrides_automatic_assignment() -> None:
    src, tgt, src_roles, tgt_roles = _two_arms(
        tgt_left_chirality=1.0, src_left_chirality=1.0,
        src_left_trans=Vector3(20, 0, 0), src_right_trans=Vector3(25, 0, 0),
    )
    forced = build_correspondence(src, {b.name for b in src}, tgt, force_pairing=[1, 0])
    mapping = forced.as_dict()
    # With the swap forced, target left now maps to the source right arm.
    assert mapping[tgt_roles["L_wrist"]] == src_roles["R_wrist"]
    assert any("forced" in w for w in forced.warnings)


def test_thumb_identified_as_abducted_finger() -> None:
    src, src_roles = _hand("S", _xform())
    tgt, tgt_roles = _hand("Bip01 R", _xform(trans=Vector3(4, 0, 0)))
    mapping = build_correspondence(src, {b.name for b in src}, tgt).as_dict()
    # The thumb slot maps thumb->thumb; a non-thumb never captures it.
    assert mapping[tgt_roles["thumb"]] == src_roles["thumb"]
    assert mapping[tgt_roles["index"]] != src_roles["thumb"]
