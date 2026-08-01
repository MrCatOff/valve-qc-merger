"""In-Blender retargeting worker.

Run *inside* Blender, never imported by the package:

    blender --background --factory-startup --python worker.py -- --job <job.json>

It is deliberately self-contained (only ``bpy``/``mathutils`` + stdlib) because
Blender's bundled interpreter does not have ``valve_qc_merger`` on its path. The
driver (:mod:`valve_qc_merger.retarget.driver`) writes a job JSON, launches this,
and reads the report JSON it emits.

Exit codes match the CLI contract (§9):
    0 ok · 3 rig discovery/correspondence failure · 4 environment/assertion failure
"""

from __future__ import annotations

import json
import math
import os
import sys
import traceback
from typing import Any

import addon_utils  # type: ignore[import-not-found]
import bpy  # type: ignore[import-not-found]
from mathutils import (  # type: ignore[import-not-found]
    Matrix as BlenderMatrix,
)
from mathutils import (
    Vector as BlenderVector,
)

# The driver puts the package's ``src`` dir in VQM_PKG_ROOT so this in-Blender
# process can import the bpy-free algorithm modules (correspondence, etc.).
_PKG_ROOT = os.environ.get("VQM_PKG_ROOT")
if _PKG_ROOT and _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from valve_qc_merger.models.geometry import Vector3  # noqa: E402
from valve_qc_merger.retarget import grip_ik, unify  # noqa: E402
from valve_qc_merger.retarget.correspondence import (  # noqa: E402
    Correspondence,
    CorrespondenceError,
    RigBone,
    build_correspondence,
)
from valve_qc_merger.retarget.pose_retarget import (  # noqa: E402
    Anchor,
    compute_bases,
    world_from_bases,
)
from valve_qc_merger.transform import (  # noqa: E402
    Transform,
    mat3_multiply,
    mat3_transpose,
    rotation_between,
)

EXIT_OK = 0
EXIT_DISCOVERY = 3
EXIT_ENV = 4


class AssertionFailure(RuntimeError):
    """A §5/§6 runtime assertion failed (maps to exit 4)."""


class DiscoveryFailure(RuntimeError):
    """Rig discovery or correspondence failed (maps to exit 3)."""


# --------------------------------------------------------------------------- #
# Phase 0 — scene setup
# --------------------------------------------------------------------------- #
def enable_bst() -> None:
    """Enable Blender Source Tools and assert its import operator registered (§7.1).

    The SMD import/export is 1:1 (the operator exposes no scale factor), so there is
    no BST scale knob to check; the scene unit scale is asserted in ``clean_scene``.
    """
    addon_utils.enable("io_scene_valvesource", default_set=True)
    if not hasattr(bpy.ops.import_scene, "smd"):
        raise AssertionFailure("Blender Source Tools did not register import_scene.smd")


def clean_scene(fps: float) -> None:
    """Empty scene, frame 0 origin, unit scale 1.0, FPS from config (§7.1)."""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    scene = bpy.context.scene
    scene.frame_start = 0
    scene.render.fps = int(round(fps))
    scene.render.fps_base = scene.render.fps / fps
    if abs(scene.unit_settings.scale_length - 1.0) > 1e-9:
        raise AssertionFailure(f"unit scale is {scene.unit_settings.scale_length}, expected 1.0")


# --------------------------------------------------------------------------- #
# Phase 1 — import + consolidation
# --------------------------------------------------------------------------- #
def _import_smd(path: str, mode: str, *, do_anim: bool = True) -> None:
    bpy.ops.import_scene.smd(
        filepath=path,
        append=mode,
        boneMode="NONE",
        makeCamera=False,
        createCollections=False,
        doAnim=do_anim,
    )


def _armatures() -> list[Any]:
    return [o for o in bpy.data.objects if o.type == "ARMATURE"]


def _only_new_armature(before: set[str]) -> Any:
    new = [o for o in _armatures() if o.name not in before]
    if len(new) != 1:
        raise AssertionFailure(f"expected exactly one new armature, got {[o.name for o in new]}")
    return new[0]


def _mesh_names() -> set[str]:
    return {o.name for o in bpy.data.objects if o.type == "MESH"}


def _only_new_mesh(before: set[str]) -> Any:
    new = [o for o in bpy.data.objects if o.type == "MESH" and o.name not in before]
    if len(new) != 1:
        raise AssertionFailure(f"expected exactly one new mesh, got {[o.name for o in new]}")
    return new[0]


class Scene:
    """Handles onto the imported, consolidated scene (§7.2)."""

    def __init__(self, src: Any, reference: Any, weapon_mesh: Any, original_mesh: Any,
                 reference_mesh: Any,
                 variant_meshes: dict[str, Any] | None = None) -> None:
        self.src = src  # animated BoneNN rig (source)
        self.reference = reference  # Bip01 rig (target)
        self.weapon_mesh = weapon_mesh  # bound to src
        self.original_mesh = original_mesh  # rebound to src (ground truth)
        self.reference_mesh = reference_mesh  # bound to reference
        self.variant_meshes = variant_meshes or {}  # bodygroup name -> mesh on reference


def import_scene(job: dict[str, Any]) -> Scene:
    """Import all inputs, attach the animation, and bind the meshes (§7.2).

    Layout after import: one animated ``SRC`` (weapon rig) carrying the weapon
    mesh *and* the original-hand mesh, plus the immutable ``reference`` rig
    carrying the reference mesh.
    """
    arms0: set[str] = set()
    meshes0 = _mesh_names()
    _import_smd(job["weapon_pv"], "NEW_ARMATURE")
    src = _only_new_armature(arms0)
    src.name = "SRC"
    weapon_mesh = _only_new_mesh(meshes0)

    # Attach the sequence animation onto the matching weapon rig (identical bones).
    _import_smd(job["sequence"]["path"], "APPEND")
    if not (src.animation_data and src.animation_data.action):
        raise AssertionFailure("animation did not attach to the source rig")

    arms1, meshes1 = {a.name for a in _armatures()}, _mesh_names()
    _import_smd(job["original_hands"], "NEW_ARMATURE")
    original_rig = _only_new_armature(arms1)
    original_mesh = _only_new_mesh(meshes1)
    _rebind(original_mesh, src)  # pose the original hand by the animation
    bpy.data.objects.remove(original_rig, do_unlink=True)

    arms2, meshes2 = {a.name for a in _armatures()}, _mesh_names()
    _import_smd(job["reference"], "NEW_ARMATURE")
    reference = _only_new_armature(arms2)
    reference_mesh = _only_new_mesh(meshes2)

    # Hand variants for $bodygroup output: each shares the reference skeleton
    # (asserted at driver level), so rebind the mesh to the reference rig and
    # drop the freshly imported duplicate rig.
    variant_meshes: dict[str, Any] = {}
    for variant_name, path in sorted(job.get("hand_variants", {}).items()):
        arms_v, meshes_v = {a.name for a in _armatures()}, _mesh_names()
        _import_smd(path, "NEW_ARMATURE")
        variant_rig = _only_new_armature(arms_v)
        variant_mesh = _only_new_mesh(meshes_v)
        _rebind(variant_mesh, reference)
        bpy.data.objects.remove(variant_rig, do_unlink=True)
        variant_meshes[variant_name] = variant_mesh

    return Scene(src, reference, weapon_mesh, original_mesh, reference_mesh,
                 variant_meshes)


def _rebind(mesh: Any, armature: Any) -> None:
    """Point the mesh's Armature modifier and parent at ``armature`` (by name)."""
    for mod in [m for m in mesh.modifiers if m.type == "ARMATURE"]:
        mod.object = armature
    mesh.parent = armature


# --------------------------------------------------------------------------- #
# §5 classification and assertions
# --------------------------------------------------------------------------- #
def bones_weighted(mesh: Any, w_min: float) -> set[str]:
    """Bone names carrying weight >= ``w_min`` from any vertex of ``mesh``."""
    names = {g.index: g.name for g in mesh.vertex_groups}
    hit: set[str] = set()
    for vert in mesh.data.vertices:
        for g in vert.groups:
            if g.weight >= w_min:
                name = names.get(g.group)
                if name is not None:
                    hit.add(name)
    return hit


def classify(scene: Scene, w_min: float) -> dict[str, list[str]]:
    """Split SRC bones into hand vs weapon by mesh weights; assert disjoint (§5)."""
    hand = bones_weighted(scene.original_mesh, w_min)
    weapon = bones_weighted(scene.weapon_mesh, w_min)
    both = sorted(hand & weapon)
    if both:
        raise AssertionFailure(f"bones weighted by both hand and weapon meshes: {both}")
    src_bones = {b.name for b in scene.src.data.bones}
    stray = (hand | weapon) - src_bones
    if stray:
        raise AssertionFailure(
            f"mesh weights reference bones absent from the source rig: {sorted(stray)}"
        )
    return {"hand": sorted(hand), "weapon": sorted(weapon)}


# --------------------------------------------------------------------------- #
# Phase 2a — geometric correspondence
# --------------------------------------------------------------------------- #
def rig_bones(armature: Any) -> list[RigBone]:
    """Extract every bone's world rest head/tail into bpy-free records."""
    mw = armature.matrix_world
    out: list[RigBone] = []
    for bone in armature.data.bones:
        head = mw @ bone.head_local
        tail = mw @ bone.tail_local
        parent = bone.parent.name if bone.parent else None
        out.append(RigBone(bone.name, parent, _v3(head), _v3(tail)))
    return out


def _v3(v: BlenderVector) -> Vector3:
    return Vector3(float(v.x), float(v.y), float(v.z))


def hand_closure(src: Any, weighted_hand: set[str], weapon: set[str]) -> set[str]:
    """Close the weighted hand set under connectivity (§5).

    Wrists and arm roots often carry no weight; add every non-weapon ancestor of a
    weighted hand bone so wrists (>=4 finger children) are present for discovery.
    """
    parent = {b.name: (b.parent.name if b.parent else None) for b in src.data.bones}
    closed = set(weighted_hand)
    for name in list(weighted_hand):
        cursor = parent.get(name)
        while cursor is not None and cursor not in weapon:
            closed.add(cursor)
            cursor = parent.get(cursor)
    return closed


def correspond(scene: Scene, classes: dict[str, list[str]], swap_arms: bool) -> Correspondence:
    """Map the reference (Bip01) hand bones onto the source (BoneNN) rig (§7.3)."""
    hand = hand_closure(scene.src, set(classes["hand"]), set(classes["weapon"]))
    force = [1, 0] if swap_arms else None
    return build_correspondence(
        rig_bones(scene.src), hand, rig_bones(scene.reference), force_pairing=force
    )


# --------------------------------------------------------------------------- #
# Phase 2b — rotation retargeting (direct matrix keying)
# --------------------------------------------------------------------------- #
def _xf(matrix: BlenderMatrix) -> Transform:
    r = matrix.to_3x3()
    rot = (
        (r[0][0], r[0][1], r[0][2]),
        (r[1][0], r[1][1], r[1][2]),
        (r[2][0], r[2][1], r[2][2]),
    )
    t = matrix.translation
    return Transform(rot, Vector3(float(t.x), float(t.y), float(t.z)))


def _bmatrix(t: Transform) -> BlenderMatrix:
    r, p = t.rotation, t.translation
    return BlenderMatrix((
        (r[0][0], r[0][1], r[0][2], p.x),
        (r[1][0], r[1][1], r[1][2], p.y),
        (r[2][0], r[2][1], r[2][2], p.z),
        (0.0, 0.0, 0.0, 1.0),
    ))


def _assert_identity_world(*armatures: Any) -> None:
    ident = BlenderMatrix.Identity(4)
    for arm in armatures:
        mw = arm.matrix_world
        if any(abs(mw[i][j] - ident[i][j]) > 1e-6 for i in range(4) for j in range(4)):
            raise AssertionFailure(f"{arm.name} has a non-identity object transform (§3)")


def _rest_transforms(armature: Any) -> dict[str, Transform]:
    return {b.name: _xf(b.matrix_local) for b in armature.data.bones}


def _parent_map(armature: Any) -> dict[str, str | None]:
    return {b.name: (b.parent.name if b.parent else None) for b in armature.data.bones}


def _anchors(corr: Correspondence, tgt_parent: dict[str, str | None]) -> list[Anchor]:
    """One translation anchor per arm: the forearm bone hanging off the held root."""
    roots = {name for name, parent in tgt_parent.items() if parent is None}
    mapping = corr.as_dict()
    anchors: list[Anchor] = []
    for side in {m.side for m in corr.maps}:
        wrist = next((m.target for m in corr.maps if m.side == side and m.role == "wrist"), None)
        if wrist is None:
            continue
        forearms = [m.target for m in corr.maps if m.side == side and m.role == "forearm"]
        proximal = [f for f in forearms if tgt_parent.get(f) in roots]
        anchor_bone = proximal[0] if proximal else wrist
        source_wrist = mapping.get(wrist)
        if source_wrist is not None:
            anchors.append(Anchor(anchor_bone, wrist, source_wrist))
    return anchors


def _children_map(tgt_parent: dict[str, str | None]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {name: [] for name in tgt_parent}
    for bone, parent in tgt_parent.items():
        if parent is not None:
            children[parent].append(bone)
    for kids in children.values():
        kids.sort()
    return children


def _finger_chains(
    mapping: dict[str, str | None], tgt_parent: dict[str, str | None], corr: Correspondence
) -> list[tuple[str, list[str], list[str]]]:
    """Each finger as (wrist, chain base..tip, curlable DOF bones) (§7.6)."""
    children = _children_map(tgt_parent)
    chains: list[tuple[str, list[str], list[str]]] = []
    for wrist in (m.target for m in corr.maps if m.role == "wrist"):
        for base in children.get(wrist, []):
            chain, cursor = [base], base
            while len(children.get(cursor, [])) == 1:
                cursor = children[cursor][0]
                chain.append(cursor)
            dof = [b for b in chain if mapping.get(b) is not None]
            if dof:
                chains.append((wrist, chain, dof))
    return chains


def _auto_hand_offset(
    by_wrist: dict[str, list[tuple[list[str], list[str]]]],
    thumb_of: dict[str, str],
    mapping: dict[str, str | None],
    tgt_rest: dict[str, Transform],
    src_rest: dict[str, Transform],
    *,
    fraction: float = 0.5,
) -> Vector3:
    """Centering: shift the hands back by ``fraction`` of the hand-length surplus.

    Anchoring at the wrists aligns one END of two differently sized hands, so the
    whole length surplus lands on the finger side and the knuckles overshoot the
    grip. Shifting back along palm-forward by (ours - original)/2 aligns the two
    hands' CENTRES instead — the surplus splits evenly behind the grip (longer
    palm) and ahead of it (longer fingers). Author-approved rule; it reproduced
    the previously hand-calibrated offset to within a few percent.

    Hand length per non-thumb finger = wrist->base span + chain segment spans,
    measured on REST positions (pose-invariant). The direction is the SOURCE
    rest pose's palm-forward axis (source wrist -> source knuckle centroid):
    the weapon's authored grip pose, identical for every sequence — deriving it
    from a posed frame would make the offset sequence-dependent (draw starts
    with the hands swung away).
    """
    diffs: list[float] = []
    dirs: list[Vector3] = []
    for wrist, group in by_wrist.items():
        src_wrist = mapping.get(wrist)
        if src_wrist is None:
            continue
        tgt_lengths: list[float] = []
        src_lengths: list[float] = []
        src_base_positions: list[Vector3] = []
        for chain, _dof in group:
            if chain[0] == thumb_of[wrist]:
                continue
            length = tgt_rest[chain[0]].translation.distance_to(
                tgt_rest[wrist].translation)
            for prev, bone in zip(chain, chain[1:], strict=False):
                length += tgt_rest[bone].translation.distance_to(
                    tgt_rest[prev].translation)
            tgt_lengths.append(length)
            src_chain = [s for s in (mapping.get(b) for b in chain) if s is not None]
            if len(src_chain) < 2:
                continue
            src_base_positions.append(src_rest[src_chain[0]].translation)
            s_len = src_rest[src_chain[0]].translation.distance_to(
                src_rest[src_wrist].translation)
            for sprev, sbone in zip(src_chain, src_chain[1:], strict=False):
                s_len += src_rest[sbone].translation.distance_to(
                    src_rest[sprev].translation)
            src_lengths.append(s_len)
        if not tgt_lengths or not src_lengths or not src_base_positions:
            continue
        cen = Vector3(
            sum(p.x for p in src_base_positions) / len(src_base_positions),
            sum(p.y for p in src_base_positions) / len(src_base_positions),
            sum(p.z for p in src_base_positions) / len(src_base_positions),
        )
        w = src_rest[src_wrist].translation
        dirs.append(_vnorm(Vector3(cen.x - w.x, cen.y - w.y, cen.z - w.z)))
        diffs.append(sum(tgt_lengths) / len(tgt_lengths)
                     - sum(src_lengths) / len(src_lengths))
    if not diffs:
        return Vector3(0.0, 0.0, 0.0)
    back = -(sum(diffs) / len(diffs)) * fraction
    u = _vnorm(Vector3(sum(d.x for d in dirs), sum(d.y for d in dirs),
                       sum(d.z for d in dirs)))
    return Vector3(u.x * back, u.y * back, u.z * back)


def _identify_thumb_chain(chains: list[list[str]], tgt_rest: dict[str, Transform]) -> int:
    """Index of the thumb chain: the base segment most abducted from the mean (§7.3.4)."""
    dirs = []
    for chain in chains:
        a = tgt_rest[chain[0]].translation
        b = tgt_rest[chain[min(1, len(chain) - 1)]].translation
        dirs.append(_vnorm(Vector3(b.x - a.x, b.y - a.y, b.z - a.z)))
    best, best_angle = 0, -1.0
    for i, di in enumerate(dirs):
        rest = [d for j, d in enumerate(dirs) if j != i]
        mean = _vnorm(Vector3(sum(d.x for d in rest), sum(d.y for d in rest),
                              sum(d.z for d in rest)))
        angle = math.acos(max(-1.0, min(1.0, di.x * mean.x + di.y * mean.y + di.z * mean.z)))
        if angle > best_angle:
            best, best_angle = i, angle
    return best


def _vnorm(v: Vector3) -> Vector3:
    length = v.length() or 1.0
    return Vector3(v.x / length, v.y / length, v.z / length)


def retarget(scene: Scene, corr: Correspondence, cfg: dict[str, Any]) -> dict[str, Any]:
    """Key the reference skeleton: rotation retarget (§7.4) + grip solve (§7.6)."""
    _assert_identity_world(scene.src, scene.reference)
    tgt_rest = _rest_transforms(scene.reference)
    tgt_parent = _parent_map(scene.reference)
    src_rest = _rest_transforms(scene.src)
    mapping = corr.as_dict()
    anchors = _anchors(corr, tgt_parent)
    src_names = {b.name for b in scene.src.data.bones}

    chains = _finger_chains(mapping, tgt_parent, corr)
    hoff = cfg.get("hand_offset")
    hand_offset = Vector3(*hoff) if hoff else None  # None => auto (half-length centering)
    auto_offset: Vector3 | None = None

    # Group the chains per wrist and mark each wrist's thumb (most abducted base).
    by_wrist: dict[str, list[tuple[list[str], list[str]]]] = {}
    for wrist, chain, dof in chains:
        by_wrist.setdefault(wrist, []).append((chain, dof))
    thumb_of: dict[str, str] = {}
    for wrist, group in by_wrist.items():
        idx = _identify_thumb_chain([chain for chain, _ in group], tgt_rest)
        thumb_of[wrist] = group[idx][0][0]

    chain_rl: dict[str, dict[str, Transform]] = {}
    for wrist, chain, _dof in chains:
        chain_rl[wrist + "|" + chain[0]] = grip_ik.rest_locals(chain, tgt_parent, tgt_rest)

    if hand_offset is None:
        hand_offset = _auto_hand_offset(
            by_wrist, thumb_of, mapping, tgt_rest, src_rest,
            fraction=float(cfg.get("hand_center_fraction", 1.0)),
        )
        auto_offset = hand_offset

    reference = scene.reference
    for pose_bone in reference.pose.bones:
        pose_bone.rotation_mode = "XYZ"

    action = scene.src.animation_data.action
    start, end = int(action.frame_range[0]), int(action.frame_range[1])
    scene_ctx = bpy.context.scene
    scene_ctx.frame_start, scene_ctx.frame_end = start, end

    aim_errors: list[float] = []
    for frame in range(start, end + 1):
        scene_ctx.frame_set(frame)
        src_pose = {name: _xf(scene.src.pose.bones[name].matrix) for name in src_names}
        bases = compute_bases(
            tgt_rest, tgt_parent, mapping, src_rest, src_pose, anchors,
            orient=corr.frames, hand_offset=hand_offset,
        )
        posed = world_from_bases(tgt_rest, tgt_parent, bases)  # open-hand world (wrist fixed)
        # Finger direction transfer: no solver. Each finger joint is aimed so its
        # segment (head -> child head) points exactly where the source finger's
        # corresponding segment points — the fingers copy the original grip pose
        # verbatim; the longer reference fingers simply extend a little further
        # along the same directions. Joints deeper than the source chain (the
        # *Nub tips and the distal joint whose source has no deeper child) keep
        # an identity basis and follow their parent straight.
        for wrist, chain, _dof in chains:
            rl = chain_rl[wrist + "|" + chain[0]]
            walk = posed[wrist]
            pos_of: dict[str, Vector3] = {}
            for i, bone in enumerate(chain):
                seat = walk.compose(rl[bone])
                pos_of[bone] = seat.translation
                basis = Transform.identity()
                src_bone = mapping.get(bone)
                src_child = mapping.get(chain[i + 1]) if i + 1 < len(chain) else None
                if src_bone is not None and src_child is not None:
                    p0 = seat.translation
                    p1 = seat.compose(rl[chain[i + 1]]).translation
                    d_ours = Vector3(p1.x - p0.x, p1.y - p0.y, p1.z - p0.z)
                    # Segment direction = head-to-child-head; BST-imported bones
                    # have synthetic tails (SMD stores none), so .tail is useless.
                    sb = scene.src.pose.bones[src_bone].head
                    sn = scene.src.pose.bones[src_child].head
                    d_src = Vector3(sn[0] - sb[0], sn[1] - sb[1], sn[2] - sb[2])
                    if d_ours.length() > 1e-6 and d_src.length() > 1e-6:
                        aim = rotation_between(_vnorm(d_ours), _vnorm(d_src))
                        basis = Transform(mat3_multiply(
                            mat3_transpose(seat.rotation),
                            mat3_multiply(aim, seat.rotation),
                        ))
                bases[bone] = basis
                walk = seat.compose(basis)
            deepest = next((b for b in reversed(chain) if mapping.get(b) is not None), None)
            if deepest is not None:
                ours_p = pos_of[deepest]
                sp = scene.src.pose.bones[mapping[deepest]].head
                aim_errors.append(math.dist(
                    (ours_p.x, ours_p.y, ours_p.z), (sp[0], sp[1], sp[2])
                ))
        for name, basis in bases.items():
            pose_bone = reference.pose.bones[name]
            pose_bone.matrix_basis = _bmatrix(basis)
            pose_bone.keyframe_insert("location", frame=frame)
            pose_bone.keyframe_insert("rotation_euler", frame=frame)

    n = len(aim_errors) or 1
    return {
        "frames": end - start + 1,
        "hand_offset_auto": (
            [auto_offset.x, auto_offset.y, auto_offset.z] if auto_offset else None
        ),
        "grip": {
            "mode": "direction-transfer",
            "fingers_per_frame": len(chains),
            # Distance of our deepest mapped finger joint from the source's — the
            # expected finger-length surplus, not a solve error.
            "joint_drift_mean": sum(aim_errors) / n,
            "joint_drift_max": max(aim_errors, default=0.0),
        },
    }


# --------------------------------------------------------------------------- #
# Phase 5 — skeleton unification and export (§7.7)
# --------------------------------------------------------------------------- #
def build_unified(scene: Scene, corr: Correspondence, classes: dict[str, list[str]]) -> list[str]:
    """Append each gun subtree into the reference armature under its wrist (§7.7).

    Returns the appended gun bone names in parent-before-child order. Weapon bone
    names, hierarchy and rest offsets are preserved; only each gun root is
    re-parented onto the assigned reference wrist. The weapon mesh is re-bound to
    the reference armature (its ``BoneNN`` vertex groups match by name unchanged).
    """
    src_bones = rig_bones(scene.src)
    hand = hand_closure(scene.src, set(classes["hand"]), set(classes["weapon"]))
    guns = unify.discover_guns(src_bones, hand, set(classes["weapon"]))
    if not guns:
        raise DiscoveryFailure("no gun subtree found to unify (§7.7)")
    wrist_of = unify.assign_wrists(guns, src_bones, corr)

    ref = scene.reference
    src = scene.src
    ref_inv = ref.matrix_world.inverted()
    ordered: list[str] = []
    bpy.context.view_layer.objects.active = ref
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        ebs = ref.data.edit_bones
        for gun in guns:
            for name in gun.bones:  # parents before children within the subtree
                src_bone = src.data.bones[name]
                world = src.matrix_world @ src_bone.matrix_local
                head = src.matrix_world @ src_bone.head_local
                tail = src.matrix_world @ src_bone.tail_local
                length = max((tail - head).length, 1e-4)
                eb = ebs.new(name)
                eb.head = (0.0, 0.0, 0.0)
                eb.tail = (0.0, 0.0, length)  # set length; matrix sets orientation
                eb.matrix = ref_inv @ world
                eb.use_deform = True
                if name == gun.root:
                    eb.parent = ebs[wrist_of[gun.root]]
                else:
                    eb.parent = ebs[src_bone.parent.name]
                ordered.append(name)
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")

    # Every bone must deform so the mesh (deform-only) and anim (all-bones) node
    # tables are byte-identical (§2.1, §7.8).
    for bone in ref.data.bones:
        bone.use_deform = True
    # Freshly created pose bones default to QUATERNION; a matrix assignment would
    # then update the quaternion while rotation_euler stays at identity, and
    # key_guns's euler keys would record dead values (guns frozen at rest).
    for name in ordered:
        ref.pose.bones[name].rotation_mode = "XYZ"
    _rebind(scene.weapon_mesh, ref)
    return ordered


def key_guns(
    scene: Scene, gun_names: list[str], start: int, end: int,
    offset: tuple[float, float, float] | None = None,
) -> None:
    """Key the appended gun bones to the source weapon animation, per frame (§7.7).

    The gun root's armature-space matrix is copied from the source (its parent
    changed, so Blender back-solves the basis); every deeper gun bone copies the
    source ``matrix_basis`` directly, since its rest offset and parent within the
    subtree are unchanged. The result reproduces the source weapon world pose,
    rigidly translated by the configured constant ``offset`` if one is set
    (§7.5/§11.2 — the same shift is applied to the grip targets in Phase 4).
    """
    ref = scene.reference
    src = scene.src
    non_euler = [n for n in gun_names if ref.pose.bones[n].rotation_mode != "XYZ"]
    if non_euler:
        raise AssertionFailure(
            f"gun bones not in XYZ rotation mode (euler keys would be dead): {non_euler}"
        )
    shift = BlenderVector((offset[0], offset[1], offset[2])) if offset else None
    roots = {name for name in gun_names if src.data.bones[name].parent is not None
             and src.data.bones[name].parent.name not in gun_names}
    scene_ctx = bpy.context.scene
    for frame in range(start, end + 1):
        scene_ctx.frame_set(frame)
        bpy.context.view_layer.update()
        for name in gun_names:  # parent-before-child order
            pose_bone = ref.pose.bones[name]
            if name in roots:
                matrix = src.pose.bones[name].matrix.copy()
                if shift is not None:
                    matrix.translation = matrix.translation + shift
                pose_bone.matrix = matrix
                bpy.context.view_layer.update()
            else:
                pose_bone.matrix_basis = src.pose.bones[name].matrix_basis.copy()
        for name in gun_names:
            pose_bone = ref.pose.bones[name]
            pose_bone.keyframe_insert("location", frame=frame)
            pose_bone.keyframe_insert("rotation_euler", frame=frame)


def _prepare_export(out_dir: str) -> None:
    scene = bpy.context.scene
    scene.vs.export_path = out_dir
    scene.vs.export_format = "SMD"
    scene.vs.smd_format = "GOLDSOURCE"  # CS 1.6 / GoldSrc weight format
    # BST's exporter runs ops.ed.undo() in its finally block when debug_value <= 1,
    # which invalidates every Python object reference we still hold. Raise it so the
    # exporter leaves the scene (and our handles) intact between the two exports.
    bpy.app.debug_value = 2


def export_mesh_smds(scene: Scene, out_dir: str, weapon_stem: str) -> dict[str, str]:
    """Export the rest-pose mesh SMDs (§7.7): merged, or per-bodygroup.

    Without hand variants: one merged reference-hands+weapon SMD, as before.
    With variants: a weapon-only SMD plus one ``hands_<name>`` SMD per variant —
    each in its own collection (BST names the SMD after the collection), all on
    the same unified skeleton so their node tables are identical.

    The armature is switched to REST so the emitted bind pose and single skeleton
    frame carry each bone's rest local transform.
    """
    ref = scene.reference
    if scene.variant_meshes:
        groups: dict[str, list[Any]] = {weapon_stem: [scene.weapon_mesh]}
        for name, mesh in scene.variant_meshes.items():
            groups[f"hands_{name}"] = [mesh]
        scene.reference_mesh.vs.export = False  # superseded by the variants
    else:
        groups = {weapon_stem: [scene.reference_mesh, scene.weapon_mesh]}

    collections = {}
    for coll_name, objects in groups.items():
        coll = bpy.data.collections.new(coll_name)
        bpy.context.scene.collection.children.link(coll)
        for ob in objects:
            for existing in list(ob.users_collection):
                existing.objects.unlink(ob)
            coll.objects.link(ob)
            ob.vs.export = True
        coll.vs.subdir = ""
        coll.vs.export = True
        collections[coll_name] = coll

    prev_pos = ref.data.pose_position
    ref.data.pose_position = "REST"
    bpy.context.view_layer.update()
    _prepare_export(out_dir)
    for coll in collections.values():
        bpy.ops.export_scene.smd(collection=coll.name)
    ref.data.pose_position = prev_pos
    bpy.context.view_layer.update()

    paths: dict[str, str] = {}
    for coll_name in collections:
        path = os.path.join(out_dir, coll_name + ".smd")
        if not os.path.exists(path):
            raise AssertionFailure(f"BST wrote no mesh SMD at {path}")
        paths[coll_name] = path
    return paths


def export_anim_smd(scene: Scene, out_dir: str, sequence: str) -> str:
    """Export the unified armature's animation as one sequence SMD (§7.7)."""
    ref = scene.reference
    ad = ref.animation_data
    ad.action.name = sequence  # BST names the SMD after the action (pre-5.2 path)
    # Blender 5.2 slotted actions: BST derives the SMD name from the slot's display
    # name, so set that too.
    if getattr(ad, "action_slot", None) is not None:
        try:
            ad.action_slot.name_display = sequence
        except (AttributeError, TypeError):
            pass
    ref.data.vs.action_selection = "CURRENT"
    ref.vs.subdir = "anims"
    ref.vs.export = True
    anim_dir = os.path.join(out_dir, "anims")
    os.makedirs(anim_dir, exist_ok=True)

    for ob in bpy.data.objects:
        ob.select_set(False)
    ref.select_set(True)
    bpy.context.view_layer.objects.active = ref
    _prepare_export(out_dir)
    bpy.ops.export_scene.smd()
    path = os.path.join(anim_dir, sequence + ".smd")
    if not os.path.exists(path):
        raise AssertionFailure(f"BST wrote no anim SMD at {path}")
    return path


def unify_and_export(
    scene: Scene, corr: Correspondence, classes: dict[str, list[str]], job: dict[str, Any]
) -> dict[str, Any]:
    """Phase 5: unify the skeleton, transfer the weapon animation, export SMDs."""
    action = scene.src.animation_data.action
    start, end = int(action.frame_range[0]), int(action.frame_range[1])
    gun_names = build_unified(scene, corr, classes)
    offset = job["config"].get("weapon_offset")
    key_guns(scene, gun_names, start, end, tuple(offset) if offset else None)

    # Delete the source rig + original hand mesh only after the weapon anim is keyed.
    bpy.data.objects.remove(scene.original_mesh, do_unlink=True)
    bpy.data.objects.remove(scene.src, do_unlink=True)

    out_dir = job["out_dir"]
    weapon_stem = job["weapon_stem"]
    result: dict[str, Any] = {"gun_bones": gun_names, "anim_smd": None, "mesh_smds": None}
    if job.get("export_mesh", False):
        result["mesh_smds"] = export_mesh_smds(scene, out_dir, weapon_stem)
    result["anim_smd"] = export_anim_smd(scene, out_dir, job["sequence"]["name"])
    return result


def assert_no_mirrors(*armatures: Any) -> None:
    """Abort if any bone rest matrix is mirrored (negative determinant, §7.3).

    A mirrored duplicate would otherwise silently invert orientations downstream.
    """
    for arm in armatures:
        for bone in arm.data.bones:
            if bone.matrix_local.to_3x3().determinant() < 0.0:
                raise DiscoveryFailure(
                    f"{arm.name} bone {bone.name} has a mirrored (negative-determinant) "
                    "rest matrix (§7.3)"
                )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run(job: dict[str, Any]) -> dict[str, Any]:
    cfg = job["config"]
    enable_bst()
    clean_scene(float(cfg["fps"]))
    scene = import_scene(job)
    assert_no_mirrors(scene.reference, scene.src)
    # Phase 3 (§7.5): zero weapon offset by default -- the weapon stays where its
    # own animation puts it and the hands come to it. A configured non-zero offset
    # is the §11.2 single constant rigid translation per weapon: applied identically
    # to the gun bones (key_guns) and the grip targets (retarget), recorded in the
    # report, and compensated in the Phase 6 weapon-pose check.
    classes = classify(scene, float(cfg["w_min"]))
    corr = correspond(scene, classes, bool(cfg.get("swap_arms", False)))
    dry_run = bool(job.get("dry_run", False))
    solved = {"frames": 0, "grip": {}} if dry_run else retarget(scene, corr, cfg)

    action = scene.src.animation_data.action
    frame_range = [int(action.frame_range[0]), int(action.frame_range[1])]
    # Snapshot everything that reads scene.src *before* Phase 5 deletes it.
    tgt_parent = _parent_map(scene.reference)
    anchor_bones = sorted({a.bone for a in _anchors(corr, tgt_parent)})
    reference_bones = [b.name for b in scene.reference.data.bones]
    counts = {
        "src_bones": len(scene.src.data.bones),
        "reference_bones": len(scene.reference.data.bones),
        "hand_bones": len(classes["hand"]),
        "weapon_bones": len(classes["weapon"]),
        "mapped_bones": sum(1 for m in corr.maps if m.source is not None),
        "held_tips": sum(1 for m in corr.maps if m.source is None),
    }

    export: dict[str, Any] = {}
    do_export = bool(job.get("export", False)) and not dry_run
    if do_export:
        export = unify_and_export(scene, corr, classes, job)

    blend_path: str | None = None
    if bool(cfg.get("save_blend", True)) and not dry_run:
        # Save the final scene next to the JSON report so it opens in Blender
        # for timeline scrubbing — after export this is the unified skeleton
        # with the keyed animation, hand variants and weapon mesh.
        if do_export:
            # BST's exporter leaves orphan duplicates behind when
            # debug_value=2 disables its undo cleanup: extra armatures AND
            # unparented mesh copies (weapon/hands ".001/.002") frozen at
            # bind pose. Those stray meshes read as "the gun floating outside
            # the hand" when the blend is opened — drop everything that is
            # not the unified reference rig or a mesh parented to it.
            for ob in list(bpy.data.objects):
                if ob.type == "ARMATURE" and ob is not scene.reference:
                    bpy.data.objects.remove(ob, do_unlink=True)
            for ob in list(bpy.data.objects):
                if ob.type == "MESH" and ob.parent is not scene.reference:
                    bpy.data.objects.remove(ob, do_unlink=True)
        blend_path = os.path.splitext(job["report"])[0] + ".blend"
        bpy.context.scene.frame_set(bpy.context.scene.frame_start)
        bpy.ops.wm.save_as_mainfile(filepath=blend_path, compress=True)

    status = "MAPPED" if dry_run else ("EXPORTED" if do_export else "RETARGETED")
    return {
        "sequence": job["sequence"]["name"],
        "status": status,
        "weapon_offset": cfg.get("weapon_offset"),
        "hand_offset": cfg.get("hand_offset"),
        "hand_offset_auto": solved.get("hand_offset_auto"),
        "blend": blend_path,
        "frames": solved["frames"],
        "grip": solved["grip"],
        "blender": bpy.app.version_string,
        "frame_range": frame_range,
        "counts": counts,
        "export": export,
        "anchor_bones": anchor_bones,
        "reference_bones": reference_bones,
        "classification": classes,
        "correspondence": {
            "score": corr.score,
            "margin": corr.margin,
            "warnings": corr.warnings,
            "map": [
                {"target": m.target, "source": m.source, "role": m.role, "side": m.side}
                for m in corr.maps
            ],
        },
    }


def _parse_job_path(argv: list[str]) -> str:
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    if "--job" not in argv:
        raise AssertionFailure("worker requires --job <path>")
    return argv[argv.index("--job") + 1]


def main() -> int:
    job_path = _parse_job_path(sys.argv)
    with open(job_path) as handle:
        job = json.load(handle)
    try:
        report = run(job)
        code = EXIT_OK
    except (DiscoveryFailure, CorrespondenceError) as exc:
        report = {"status": "FAIL", "error": str(exc), "kind": "discovery"}
        code = EXIT_DISCOVERY
    except AssertionFailure as exc:
        report = {"status": "FAIL", "error": str(exc), "kind": "assertion"}
        code = EXIT_ENV
    except Exception as exc:  # noqa: BLE001 - report any crash to the driver
        report = {"status": "FAIL", "error": str(exc), "kind": "crash",
                  "traceback": traceback.format_exc()}
        code = EXIT_ENV
    with open(job["report"], "w") as handle:
        json.dump(report, handle, indent=2)
    return code


if __name__ == "__main__":
    sys.exit(main())
