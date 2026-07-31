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
from valve_qc_merger.transform import Transform  # noqa: E402

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
                 reference_mesh: Any) -> None:
        self.src = src  # animated BoneNN rig (source)
        self.reference = reference  # Bip01 rig (target)
        self.weapon_mesh = weapon_mesh  # bound to src
        self.original_mesh = original_mesh  # rebound to src (ground truth)
        self.reference_mesh = reference_mesh  # bound to reference


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

    return Scene(src, reference, weapon_mesh, original_mesh, reference_mesh)


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


def _finger_limits(cfg: dict[str, Any]) -> tuple[list[tuple[float, float]], tuple[float, float]]:
    """Scalar hinge limits (rad): per-depth [MCP, PIP, DIP] plus the thumb's (§7.6)."""
    solver = cfg["solver"]

    def lim(pair: Any) -> tuple[float, float]:
        return (math.radians(float(pair[0])), math.radians(float(pair[1])))

    depth = [lim(solver["hinge_mcp"]), lim(solver["hinge_pip"]), lim(solver["hinge_dip"])]
    return depth, lim(solver["hinge_thumb"])


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


def _vcross(a: Vector3, b: Vector3) -> Vector3:
    return Vector3(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x)


def _knuckle_axis(bases: list[Vector3]) -> Vector3:
    """Direction between the two farthest finger bases — the flexion hinge axis."""
    best = Vector3(1.0, 0.0, 0.0)
    best_d = -1.0
    for i in range(len(bases)):
        for j in range(i + 1, len(bases)):
            v = Vector3(bases[i].x - bases[j].x, bases[i].y - bases[j].y,
                        bases[i].z - bases[j].z)
            d = v.length()
            if d > best_d:
                best_d, best = d, v
    return _vnorm(best)


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
    depth_limits, thumb_limit = _finger_limits(cfg)
    offset = cfg.get("weapon_offset")
    target_shift = Vector3(*offset) if offset else Vector3(0.0, 0.0, 0.0)

    # Group the chains per wrist and mark each wrist's thumb (most abducted base).
    by_wrist: dict[str, list[tuple[list[str], list[str]]]] = {}
    for wrist, chain, dof in chains:
        by_wrist.setdefault(wrist, []).append((chain, dof))
    thumb_of: dict[str, str] = {}
    for wrist, group in by_wrist.items():
        idx = _identify_thumb_chain([chain for chain, _ in group], tgt_rest)
        thumb_of[wrist] = group[idx][0][0]

    chain_rl: dict[str, dict[str, Transform]] = {}
    chain_lim: dict[str, dict[str, tuple[float, float]]] = {}
    for wrist, chain, dof in chains:
        key = wrist + "|" + chain[0]
        chain_rl[key] = grip_ik.rest_locals(chain, tgt_parent, tgt_rest)
        if chain[0] == thumb_of[wrist]:
            chain_lim[key] = {b: thumb_limit for b in dof}
        else:
            chain_lim[key] = {
                b: depth_limits[min(k, len(depth_limits) - 1)] for k, b in enumerate(dof)
            }
    iters = int(cfg["solver"]["max_iterations"])
    max_step = math.radians(float(cfg["solver"]["max_step_degrees"]))

    reference = scene.reference
    for pose_bone in reference.pose.bones:
        pose_bone.rotation_mode = "XYZ"

    action = scene.src.animation_data.action
    start, end = int(action.frame_range[0]), int(action.frame_range[1])
    scene_ctx = bpy.context.scene
    scene_ctx.frame_start, scene_ctx.frame_end = start, end

    warm: dict[str, dict[str, float]] = {}
    curl_sign: dict[str, float] = {}
    prev_axis: dict[str, Vector3] = {}
    tip_errors: list[float] = []
    for frame in range(start, end + 1):
        scene_ctx.frame_set(frame)
        src_pose = {name: _xf(scene.src.pose.bones[name].matrix) for name in src_names}
        bases = compute_bases(
            tgt_rest, tgt_parent, mapping, src_rest, src_pose, anchors, orient=corr.frames
        )
        posed = world_from_bases(tgt_rest, tgt_parent, bases)  # open-hand world (wrist fixed)
        # Per-arm hinge axis: the posed knuckle line of the non-thumb finger bases,
        # oriented away from the thumb so its sign is stable across frames.
        knuckle: dict[str, Vector3] = {}
        for wrist, group in by_wrist.items():
            non_thumb = [posed[chain[0]].translation for chain, _ in group
                         if chain[0] != thumb_of[wrist]]
            axis = _knuckle_axis(non_thumb)
            wrist_pos = posed[wrist].translation
            thumb_pos = posed[thumb_of[wrist]].translation
            to_thumb = Vector3(thumb_pos.x - wrist_pos.x, thumb_pos.y - wrist_pos.y,
                               thumb_pos.z - wrist_pos.z)
            if axis.x * to_thumb.x + axis.y * to_thumb.y + axis.z * to_thumb.z > 0.0:
                axis = Vector3(-axis.x, -axis.y, -axis.z)
            knuckle[wrist] = axis
        for wrist, chain, dof in chains:
            key = wrist + "|" + chain[0]
            base_parent_world = posed[wrist]
            tip = _v3(scene.src.pose.bones[mapping[dof[-1]]].tail)
            target_tip = Vector3(tip.x + target_shift.x, tip.y + target_shift.y,
                                 tip.z + target_shift.z)
            if chain[0] == thumb_of[wrist]:
                # Thumb: hinge in the base-to-target arc plane (planar curl straight
                # toward its own contact point — cannot fold the wrong way). The
                # plane normal is recomputed per frame, so near-collinear frames
                # could flip its hemisphere and pop the joint by ~180°: keep the
                # previous frame's axis when degenerate and hemisphere-align to it
                # otherwise, so the hinge turns continuously across the sequence.
                base = posed[chain[0]].translation
                open_tip = posed[chain[-1]].translation
                axis = _vcross(
                    Vector3(open_tip.x - base.x, open_tip.y - base.y, open_tip.z - base.z),
                    Vector3(target_tip.x - base.x, target_tip.y - base.y,
                            target_tip.z - base.z),
                )
                if axis.length() < 1e-6:
                    axis = prev_axis.get(key, knuckle[wrist])
                else:
                    axis = _vnorm(axis)
                previous = prev_axis.get(key)
                if previous is not None and (
                    axis.x * previous.x + axis.y * previous.y + axis.z * previous.z
                ) < 0.0:
                    axis = Vector3(-axis.x, -axis.y, -axis.z)
                prev_axis[key] = axis
            else:
                axis = knuckle[wrist]
            if key not in curl_sign:
                curl_sign[key] = grip_ik.calibrate_axis_sign(
                    chain, dof, base_parent_world, chain_rl[key], target_tip, axis
                )
            sign = curl_sign[key]
            axis = Vector3(axis.x * sign, axis.y * sign, axis.z * sign)
            finger, angles = grip_ik.solve_finger(
                chain, dof, base_parent_world, chain_rl[key], target_tip,
                chain_lim[key], axis=axis, warm_start=warm.get(key),
                max_step=max_step, iterations=iters,
            )
            warm[key] = angles
            bases.update(finger)
            tip_errors.append(
                grip_ik.tip_error(chain, base_parent_world, chain_rl[key], finger, target_tip)
            )
        for name, basis in bases.items():
            pose_bone = reference.pose.bones[name]
            pose_bone.matrix_basis = _bmatrix(basis)
            pose_bone.keyframe_insert("location", frame=frame)
            pose_bone.keyframe_insert("rotation_euler", frame=frame)

    n = len(tip_errors) or 1
    return {
        "frames": end - start + 1,
        "grip": {
            "fingers_per_frame": len(chains),
            "tip_error_mean": sum(tip_errors) / n,
            "tip_error_max": max(tip_errors, default=0.0),
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


def export_mesh_smd(scene: Scene, out_dir: str, weapon_stem: str) -> str:
    """Export reference hands + weapon merged into one rest-pose mesh SMD (§7.7).

    The armature is switched to REST so the emitted bind pose and single skeleton
    frame carry each bone's rest local transform.
    """
    ref = scene.reference
    coll = bpy.data.collections.new(weapon_stem)
    bpy.context.scene.collection.children.link(coll)
    for ob in (scene.reference_mesh, scene.weapon_mesh):
        for existing in list(ob.users_collection):
            existing.objects.unlink(ob)
        coll.objects.link(ob)
        ob.vs.export = True
    coll.vs.subdir = ""
    coll.vs.export = True

    prev_pos = ref.data.pose_position
    ref.data.pose_position = "REST"
    bpy.context.view_layer.update()
    _prepare_export(out_dir)
    bpy.ops.export_scene.smd(collection=coll.name)
    ref.data.pose_position = prev_pos
    bpy.context.view_layer.update()
    # BST names the file after the collection.
    path = os.path.join(out_dir, weapon_stem + ".smd")
    if not os.path.exists(path):
        raise AssertionFailure(f"BST wrote no mesh SMD at {path}")
    return path


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
    result: dict[str, Any] = {"gun_bones": gun_names, "anim_smd": None, "mesh_smd": None}
    if job.get("export_mesh", False):
        result["mesh_smd"] = export_mesh_smd(scene, out_dir, weapon_stem)
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

    status = "MAPPED" if dry_run else ("EXPORTED" if do_export else "RETARGETED")
    return {
        "sequence": job["sequence"]["name"],
        "status": status,
        "weapon_offset": cfg.get("weapon_offset"),
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
