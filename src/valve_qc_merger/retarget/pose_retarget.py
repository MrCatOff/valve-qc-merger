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
from valve_qc_merger.retarget.correspondence import _side_of
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
    orient: dict[str, tuple[Matrix3, Matrix3]] | None = None,
    hand_offset: Vector3 | dict[str, Vector3] | None = None,
) -> dict[str, Transform]:
    """Return each target bone's ``matrix_basis`` for one frame (§7.4).

    ``orient`` maps a bone to its (source, target) anatomical rest frames. Because
    the reference hands are a flat T-pose whose bind orientation is unrelated to
    the grip, a plain delta-from-rest retarget keeps them splayed. When a bone has
    an anatomical frame, the correction re-expresses the source's motion in that
    frame so the target hand adopts the source's *absolute* orientation (arm
    pointing along the grip), independent of the T-pose. Bones without a frame fall
    back to the delta form.

    ``hand_offset`` is a constant world translation added to every anchor's wrist
    target: the whole hand lands at ``source wrist + offset`` instead of exactly on
    the source wrist, compensating a hand-size mismatch while the weapon (and the
    grip contact points on it) stay exactly where the animation puts them. Only the
    anchor bone's pose translation changes — nodes, triangles and every other
    bone's local translation are untouched. World-space and identical for all
    arms, so lateral (X) shifts are asymmetric for mirrored dual-wield arms.
    """
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
            correction = _correction(
                src_rest[source].rotation, tgt_rest[bone].rotation,
                orient.get(bone) if orient else None,
            )
            desired_world = mat3_multiply(src_pose[source].rotation, correction)
            basis_rot = mat3_multiply(mat3_transpose(seat.rotation), desired_world)
        else:
            basis_rot = _IDENTITY3

        this = Transform(basis_rot, Vector3(0.0, 0.0, 0.0))
        basis[bone] = this
        posed[bone] = seat.compose(this)

    _solve_anchors(tgt_rest, tgt_parent, posed, basis, src_pose, anchors, hand_offset)
    return basis


def _correction(
    rest_src: Matrix3, rest_tgt: Matrix3, frames: tuple[Matrix3, Matrix3] | None
) -> Matrix3:
    """Rotation C such that ``R_pose_tgt = R_pose_src . C`` (§7.4).

    Delta form (no frame): ``C = R_rest_src^-1 . R_rest_tgt`` -- carries the
    source's motion onto the target's own rest orientation. Anatomical form: insert
    ``F_src . F_tgt^-1`` so the target adopts the source's absolute hand orientation
    rather than the reference T-pose. The two agree when the rest frames coincide.
    """
    if frames is None:
        return mat3_multiply(mat3_transpose(rest_src), rest_tgt)
    f_src, f_tgt = frames
    inner = mat3_multiply(f_src, mat3_multiply(mat3_transpose(f_tgt), rest_tgt))
    return mat3_multiply(mat3_transpose(rest_src), inner)


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
    hand_offset: Vector3 | dict[str, Vector3] | None = None,
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
        offset: Vector3 | None
        if isinstance(hand_offset, dict):
            side = "L" if _side_of(anchor.wrist) == "L" else "R"
            offset = hand_offset.get(side)
        else:
            offset = hand_offset
        if offset is not None:
            target = Vector3(target.x + offset.x, target.y + offset.y,
                             target.z + offset.z)
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
