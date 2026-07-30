"""Tests for the hand/gun triangle-intersection collision detector."""

from __future__ import annotations

from pathlib import Path

import pytest

from valve_qc_merger.collision import _triangles_intersect, grip_collisions
from valve_qc_merger.correspondence import build_hand_correspondences
from valve_qc_merger.kinematics import world_transforms
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget import HandGraft
from valve_qc_merger.transform import Transform


def _elite() -> Path:
    return Path(__file__).resolve().parents[1] / "tmp" / "pistols" / "view" / "v_elite"


def _hands() -> Path:
    return Path(__file__).resolve().parents[1] / "tmp" / "hands"


requires_elite = pytest.mark.skipif(
    not (_elite().exists() and _hands().exists()), reason="v_elite sample not available"
)


def test_triangle_intersection_predicate() -> None:
    # A flat triangle in the z=0 plane; a second triangle whose edge pierces it.
    flat = ((0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 2.0, 0.0))
    crossing = ((0.5, 0.5, -1.0), (0.5, 0.5, 1.0), (1.5, 0.5, 1.0))
    assert _triangles_intersect(flat, crossing)
    # Same shape lifted well above the plane: no intersection.
    above = ((0.5, 0.5, 1.0), (0.5, 0.5, 2.0), (1.5, 0.5, 2.0))
    assert not _triangles_intersect(flat, above)
    # Coplanar but disjoint (shifted along x): no piercing edge.
    coplanar = ((5.0, 0.0, 0.0), (7.0, 0.0, 0.0), (5.0, 2.0, 0.0))
    assert not _triangles_intersect(flat, coplanar)


@requires_elite
def test_grip_collisions_detects_and_clears() -> None:
    weapon = parse_smd_file(_elite() / "v_elite-PV.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    links = build_hand_correspondences(weapon, hand)
    animation = parse_smd_file(_elite() / "v_elite_anims" / "draw.smd")

    graft = HandGraft(weapon, hand, links, finger_ik=True)
    hand_smd = graft.reference_smd()
    gun_smd = graft.weapon_reference_smd()
    out = graft.retarget_animation(animation)
    frame = next(f for f in out.frames if f.time == 32)
    world = world_transforms(out.nodes, frame)

    clipping = grip_collisions(hand_smd, gun_smd, world)
    # The fuller reference-hand mesh clips the weapon at the grip.
    assert sum(c.triangles for c in clipping.values()) > 0
    assert all(region.split("_")[0] in ("L", "R") for region in clipping)
    # Real, finite penetration is reported in both directions, and max_depth is
    # the larger of the two.
    assert 0.0 < max(c.max_depth for c in clipping.values()) < 5.0
    assert all(c.max_depth == max(c.into_gun, c.into_hand) for c in clipping.values())

    # Push the gun bones 1000 units away: nothing should clip any more.
    gun_bones = {v.bone for tri in gun_smd.triangles for v in tri.vertices}
    far = dict(world)
    for bone in gun_bones & world.keys():
        t = world[bone]
        moved = Vector3(t.translation.x + 1000.0, t.translation.y, t.translation.z)
        far[bone] = Transform(t.rotation, moved)
    assert not grip_collisions(hand_smd, gun_smd, far)
