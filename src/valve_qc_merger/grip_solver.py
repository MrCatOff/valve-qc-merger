"""Solve per-finger retracts that stop the reference-hand mesh clipping the gun.

The reference hand's fingers are fleshier and longer than the weapon's, so at the
authored grip the finger *mesh* pokes through the weapon even though the bones are
placed correctly (see :mod:`collision`). Retracting a finger's grip contact toward
the hand pulls its longer mesh back until it stops clipping.

:func:`solve_grip` finds, per finger, the *smallest* retract that clears the clip
across sampled frames -- smallest so the finger stays in contact with the grip
rather than pulling off it. The clip is measured with the fast ``thru-gun`` pass
of the collision detector, so the search is the objective, verified every step.
"""

from __future__ import annotations

from valve_qc_merger.collision import grip_collisions
from valve_qc_merger.correspondence import HandLink
from valve_qc_merger.kinematics import world_transforms
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import Smd
from valve_qc_merger.retarget import HandGraft
from valve_qc_merger.transform import Transform

_ZERO = Vector3(0.0, 0.0, 0.0)
_PARTS = ("thumb", "index", "middle", "ring", "pinky")


def _region(base_name: str) -> str:
    side = "L" if "_L_" in base_name else "R"
    for i, part in enumerate(_PARTS):
        if base_name.endswith(f"Finger{i}"):
            return f"{side}_{part}"
    return f"{side}_palm"


def solve_grip(
    weapon: Smd,
    hand: Smd,
    links: list[HandLink],
    animation: Smd,
    *,
    sample_times: list[int],
    tolerance: float = 0.15,
    max_retract: float = 1.6,
    step: float = 0.2,
    index_curl: float = 0.0,
    grip_slide: Vector3 = _ZERO,
    weapon_offset: Vector3 = _ZERO,
    offsets: dict[str, Transform] | None = None,
) -> dict[str, float]:
    """Return ``{finger_base_bone: retract}`` that clears the grip clip.

    ``sample_times`` are the animation frame times the grip is checked on (a small
    spread over the gripping range). Fingers are cleared one at a time; only
    non-zero retracts are returned, ready to pass to ``HandGraft(retracts=...)``.
    """
    names = {node.index: node.name for node in hand.nodes}
    bases = [names[target.joints[0]] for link in links for _, target in link.finger_pairs]
    wanted = set(sample_times)
    frames = [frame for frame in animation.frames if frame.time in wanted]
    sub = Smd(version=animation.version, nodes=animation.nodes, frames=frames)

    retracts: dict[str, float] = {}

    def worst_thru_gun() -> dict[str, float]:
        graft = HandGraft(
            weapon,
            hand,
            links,
            offsets,
            weapon_offset,
            True,
            index_curl,
            grip_slide,
            retracts,
        )
        hand_smd = graft.reference_smd()
        gun_smd = graft.weapon_reference_smd()
        out = graft.retarget_animation(sub)
        worst: dict[str, float] = {}
        for frame in out.frames:
            world = world_transforms(out.nodes, frame)
            clips = grip_collisions(hand_smd, gun_smd, world, measure_into_hand=False)
            for region, clip in clips.items():
                worst[region] = max(worst.get(region, 0.0), clip.into_gun)
        return worst

    for base in bases:
        region = _region(base)
        chosen = 0.0
        value = 0.0
        while value <= max_retract + 1e-9:
            retracts[base] = value
            if worst_thru_gun().get(region, 0.0) <= tolerance:
                chosen = value
                break
            chosen = value
            value += step
        retracts[base] = chosen

    return {base: value for base, value in retracts.items() if value > 1e-9}


__all__ = ["solve_grip"]
