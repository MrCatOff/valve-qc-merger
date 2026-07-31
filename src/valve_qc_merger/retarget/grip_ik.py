"""Finger-curl grip solve (Phase 4, §7.6).

Pure Python (no ``bpy``): curl each reference finger onto the weapon so its tip
reaches where the *original* finger tip rested, in the already-retargeted pose.
The reference fingers are longer than the original's, so targeting the (nearer)
original tip makes the extra length wrap further around the grip instead of poking
through it -- exactly the spec's intent.

The solver is Cyclic Coordinate Descent over the finger chain's joints, expressed
in each joint's local ``matrix_basis`` (so it composes with Phase 2b's output):

    world(bone) = world(parent) . rest_local(bone) . basis(bone)

Each iteration swings each joint (distal-to-proximal) to bring the tip toward the
target, then clamps the joint to its flexion-dominant limits. Warm-starting from
the previous frame's solution keeps the motion temporally coherent and cheap.
"""

from __future__ import annotations

from dataclasses import dataclass

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.transform import (
    Transform,
    euler_to_matrix,
    mat3_multiply,
    mat3_transpose,
    matrix_to_euler,
    rotation_between,
)


@dataclass(frozen=True)
class Limit:
    """Per-axis Euler limits in radians (min, max)."""

    lo: tuple[float, float, float]
    hi: tuple[float, float, float]


def rest_locals(chain: list[str], parent_of: dict[str, str | None],
                tgt_rest: dict[str, Transform]) -> dict[str, Transform]:
    """Parent-relative rest transform for each bone in a finger chain."""
    out: dict[str, Transform] = {}
    for bone in chain:
        parent = parent_of[bone]
        parent_rest = tgt_rest[parent] if parent is not None else Transform.identity()
        out[bone] = parent_rest.inverse().compose(tgt_rest[bone])
    return out


def _fk(chain: list[str], base_parent: Transform, rest_local: dict[str, Transform],
        basis: dict[str, Transform]) -> dict[str, Transform]:
    posed: dict[str, Transform] = {}
    prev = base_parent
    for bone in chain:
        posed[bone] = prev.compose(rest_local[bone]).compose(basis[bone])
        prev = posed[bone]
    return posed


def _clamp_basis(rot: object, limit: Limit) -> Transform:
    euler = matrix_to_euler(rot)  # type: ignore[arg-type]
    clamped = Vector3(
        min(limit.hi[0], max(limit.lo[0], euler.x)),
        min(limit.hi[1], max(limit.lo[1], euler.y)),
        min(limit.hi[2], max(limit.lo[2], euler.z)),
    )
    return Transform(euler_to_matrix(clamped))


def solve_finger(
    chain: list[str],
    dof: list[str],
    base_parent_world: Transform,
    rest_local: dict[str, Transform],
    target_tip: Vector3,
    limits: dict[str, Limit],
    *,
    warm_start: dict[str, Transform] | None = None,
    iterations: int = 12,
    tolerance: float = 1e-3,
) -> dict[str, Transform]:
    """CCD-solve one finger chain; return the basis for every chain bone (§7.6).

    ``chain`` is base..tip (the tip bone, e.g. a held ``*Nub``, is not a DOF);
    ``dof`` is the curlable subset (bones with a source). The tip position is the
    world head of the last chain bone.
    """
    basis: dict[str, Transform] = {b: Transform.identity() for b in chain}
    if warm_start:
        for b in chain:
            if b in warm_start:
                basis[b] = warm_start[b]

    index = {b: i for i, b in enumerate(chain)}
    for _ in range(iterations):
        posed = _fk(chain, base_parent_world, rest_local, basis)
        tip = posed[chain[-1]].translation
        if tip.distance_to(target_tip) < tolerance:
            break
        for joint in reversed(dof):
            posed = _fk(chain, base_parent_world, rest_local, basis)
            tip = posed[chain[-1]].translation
            pivot = posed[joint].translation
            to_tip = Vector3(tip.x - pivot.x, tip.y - pivot.y, tip.z - pivot.z)
            to_target = Vector3(
                target_tip.x - pivot.x, target_tip.y - pivot.y, target_tip.z - pivot.z
            )
            if to_tip.length() < 1e-6 or to_target.length() < 1e-6:
                continue
            swing = rotation_between(to_tip, to_target)

            i = index[joint]
            parent_world = base_parent_world if i == 0 else posed[chain[i - 1]]
            seat_rot = parent_world.compose(rest_local[joint]).rotation
            # basis' = seat^-1 . swing . seat . basis  (encode the world swing locally)
            new_rot = mat3_multiply(
                mat3_transpose(seat_rot),
                mat3_multiply(swing, mat3_multiply(seat_rot, basis[joint].rotation)),
            )
            limit = limits.get(joint)
            basis[joint] = _clamp_basis(new_rot, limit) if limit else Transform(new_rot)
    return basis


def tip_error(
    chain: list[str], base_parent_world: Transform, rest_local: dict[str, Transform],
    basis: dict[str, Transform], target_tip: Vector3,
) -> float:
    """Distance from the solved fingertip to its target (validation metric)."""
    posed = _fk(chain, base_parent_world, rest_local, basis)
    return posed[chain[-1]].translation.distance_to(target_tip)


__all__ = ["Limit", "rest_locals", "solve_finger", "tip_error"]
