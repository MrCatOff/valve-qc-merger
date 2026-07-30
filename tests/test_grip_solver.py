"""Test the collision-driven grip solver."""

from __future__ import annotations

from pathlib import Path

import pytest

from valve_qc_merger.collision import grip_collisions
from valve_qc_merger.correspondence import build_hand_correspondences
from valve_qc_merger.grip_solver import solve_grip
from valve_qc_merger.kinematics import world_transforms
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget import HandGraft


def _elite() -> Path:
    return Path(__file__).resolve().parents[1] / "tmp" / "pistols" / "view" / "v_elite"


def _hands() -> Path:
    return Path(__file__).resolve().parents[1] / "tmp" / "hands"


requires_elite = pytest.mark.skipif(
    not (_elite().exists() and _hands().exists()), reason="v_elite sample not available"
)


@requires_elite
def test_solve_grip_clears_the_clipping() -> None:
    weapon = parse_smd_file(_elite() / "v_elite-PV.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    links = build_hand_correspondences(weapon, hand)
    animation = parse_smd_file(_elite() / "v_elite_anims" / "draw.smd")
    samples = [16, 32]

    def worst_thru_gun(retracts: dict[str, float]) -> float:
        graft = HandGraft(weapon, hand, links, finger_ik=True, retracts=retracts)
        hand_smd = graft.reference_smd()
        gun_smd = graft.weapon_reference_smd()
        out = graft.retarget_animation(animation)
        worst = 0.0
        for time in samples:
            world = world_transforms(out.nodes, next(f for f in out.frames if f.time == time))
            clips = grip_collisions(hand_smd, gun_smd, world, measure_into_hand=False)
            worst = max(worst, *(c.into_gun for c in clips.values()), 0.0)
        return worst

    before = worst_thru_gun({})
    retracts = solve_grip(weapon, hand, links, animation, sample_times=samples, tolerance=0.15)
    after = worst_thru_gun(retracts)

    assert before > 0.5  # the fuller mesh clips the weapon before solving
    assert after <= 0.15  # ...and the solver retracts each finger until it does not
    assert retracts  # at least one finger needed a retract
    assert all(v > 0.0 for v in retracts.values())
