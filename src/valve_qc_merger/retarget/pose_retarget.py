"""Rotation retargeting math (Phase 2b, §7.4).

Pure Python (no ``bpy``): given the two rigs' world rest poses and the source
rig's world pose for one frame, compute each target bone's ``matrix_basis`` (its
local pose delta) directly -- no constraints, no visual-keying bake, no depsgraph
round-trip. The worker reads the source pose out of Blender, calls
:func:`compute_bases`, writes the returned bases and keyframes them.

The retarget applies the *source's* world rotation delta-from-rest to the
*target's* rest orientation:

    D_src(b)      = R_pose_src(s) . R_rest_src(s)^-1        # source world delta
    R_pose_tgt(b) = D_src(b) . R_rest_tgt(b)               # desired world rotation

so the target keeps its own bone lengths and proportions and only borrows motion.
Bones with no source (held reference ``*Nub`` tips) keep an identity basis, so
they simply follow their parent. Exactly one bone per arm -- the anchor -- also
carries translation, solved so the target wrist lands on the source wrist.
"""

from __future__ import annotations

from dataclasses import dataclass

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.transform import (
    Matrix3,
    Transform,
    mat3_multiply,
    mat3_transpose,
)

_IDENTITY3: Matrix3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


@dataclass(frozen=True)
class Anchor:
    """One arm's translation anchor: solve ``wrist`` onto ``source_wrist``."""

    bone: str  # target bone that carries the translation (the arm's proximal forearm)
    wrist: str  # target wrist bone whose world head must land on the source wrist
    source_wrist: str  # source wrist bone providing the target head position


def topo_order(parent: dict[str, str | None]) -> list[str]:
    """Bones ordered parents-before-children (stable by name within a level)."""

    def depth(name: str) -> int:
        d = 0
        cursor = parent.get(name)
        while cursor is not None:
            d += 1
            cursor = parent.get(cursor)
        return d

    return sorted(parent, key=lambda n: (depth(n), n))


def compute_bases(
    tgt_rest: dict[str, Transform],
    tgt_parent: dict[str, str | None],
    mapping: dict[str, str | None],
    src_rest: dict[str, Transform],
    src_pose: dict[str, Transform],
    anchors: list[Anchor],
) -> dict[str, Transform]:
    """Return each target bone's ``matrix_basis`` for one frame (§7.4)."""
    order = topo_order(tgt_parent)
    posed: dict[str, Transform] = {}
    basis: dict[str, Transform] = {}

    for bone in order:
        parent = tgt_parent.get(bone)
        parent_posed = posed[parent] if parent else Transform.identity()
        parent_rest = tgt_rest[parent] if parent else Transform.identity()
        rest_local = parent_rest.inverse().compose(tgt_rest[bone])
        seat = parent_posed.compose(rest_local)  # world of this bone at identity basis

        source = mapping.get(bone)
        if source is not None and source in src_pose and source in src_rest:
            delta = mat3_multiply(
                src_pose[source].rotation, mat3_transpose(src_rest[source].rotation)
            )
            desired_world = mat3_multiply(delta, tgt_rest[bone].rotation)
            basis_rot = mat3_multiply(mat3_transpose(seat.rotation), desired_world)
        else:
            basis_rot = _IDENTITY3

        this = Transform(basis_rot, Vector3(0.0, 0.0, 0.0))
        basis[bone] = this
        posed[bone] = seat.compose(this)

    _solve_anchors(tgt_rest, tgt_parent, posed, basis, src_pose, anchors)
    return basis


def world_from_bases(
    tgt_rest: dict[str, Transform],
    tgt_parent: dict[str, str | None],
    basis: dict[str, Transform],
) -> dict[str, Transform]:
    """Compose world poses from bases the way Blender's hierarchy would (§7.4)."""
    posed: dict[str, Transform] = {}
    for bone in topo_order(tgt_parent):
        parent = tgt_parent.get(bone)
        parent_posed = posed[parent] if parent else Transform.identity()
        parent_rest = tgt_rest[parent] if parent else Transform.identity()
        rest_local = parent_rest.inverse().compose(tgt_rest[bone])
        posed[bone] = parent_posed.compose(rest_local).compose(basis[bone])
    return posed


def _solve_anchors(
    tgt_rest: dict[str, Transform],
    tgt_parent: dict[str, str | None],
    posed: dict[str, Transform],
    basis: dict[str, Transform],
    src_pose: dict[str, Transform],
    anchors: list[Anchor],
) -> None:
    """Set each anchor's basis translation so its wrist lands on the source wrist.

    The anchor's basis translation shifts its whole subtree rigidly, so the wrist
    head is affine in it and one solve is exact; child bases are local and so are
    unaffected -- no second pass is needed.
    """
    for anchor in anchors:
        if anchor.source_wrist not in src_pose or anchor.wrist not in posed:
            continue
        target = src_pose[anchor.source_wrist].translation
        current = posed[anchor.wrist].translation
        delta = Vector3(target.x - current.x, target.y - current.y, target.z - current.z)

        parent = tgt_parent.get(anchor.bone)
        parent_posed = posed[parent] if parent else Transform.identity()
        parent_rest = tgt_rest[parent] if parent else Transform.identity()
        rest_local = parent_rest.inverse().compose(tgt_rest[anchor.bone])
        frame = mat3_multiply(parent_posed.rotation, rest_local.rotation)
        local_delta = Transform(mat3_transpose(frame)).rotate_vector(delta)
        basis[anchor.bone] = Transform(basis[anchor.bone].rotation, local_delta)


__all__ = ["Anchor", "compute_bases", "topo_order", "world_from_bases"]
