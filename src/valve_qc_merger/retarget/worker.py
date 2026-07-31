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
from valve_qc_merger.retarget import grip_ik  # noqa: E402
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


def _finger_limits(cfg: dict[str, Any]) -> list[grip_ik.Limit]:
    """MCP/PIP/DIP joint limits (deg -> rad) by chain depth (§7.6)."""
    solver = cfg["solver"]

    def lim(d: dict[str, Any]) -> grip_ik.Limit:
        return grip_ik.Limit(
            tuple(math.radians(x) for x in d["min"]),  # type: ignore[arg-type]
            tuple(math.radians(x) for x in d["max"]),  # type: ignore[arg-type]
        )

    return [lim(solver["limit_mcp"]), lim(solver["limit_pip"]), lim(solver["limit_dip"])]


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
    depth_limits = _finger_limits(cfg)
    chain_rl: dict[str, dict[str, Transform]] = {}
    chain_lim: dict[str, dict[str, grip_ik.Limit]] = {}
    for wrist, chain, dof in chains:
        key = wrist + "|" + chain[0]
        chain_rl[key] = grip_ik.rest_locals(chain, tgt_parent, tgt_rest)
        chain_lim[key] = {b: depth_limits[min(k, len(depth_limits) - 1)] for k, b in enumerate(dof)}
    iters = int(cfg["solver"]["max_iterations"])

    reference = scene.reference
    for pose_bone in reference.pose.bones:
        pose_bone.rotation_mode = "XYZ"

    action = scene.src.animation_data.action
    start, end = int(action.frame_range[0]), int(action.frame_range[1])
    scene_ctx = bpy.context.scene
    scene_ctx.frame_start, scene_ctx.frame_end = start, end

    warm: dict[str, dict[str, Transform]] = {}
    tip_errors: list[float] = []
    for frame in range(start, end + 1):
        scene_ctx.frame_set(frame)
        src_pose = {name: _xf(scene.src.pose.bones[name].matrix) for name in src_names}
        bases = compute_bases(
            tgt_rest, tgt_parent, mapping, src_rest, src_pose, anchors, orient=corr.frames
        )
        posed = world_from_bases(tgt_rest, tgt_parent, bases)  # open-hand world (wrist fixed)
        for wrist, chain, dof in chains:
            key = wrist + "|" + chain[0]
            base_parent_world = posed[wrist]
            target_tip = _v3(scene.src.pose.bones[mapping[dof[-1]]].tail)
            finger = grip_ik.solve_finger(
                chain, dof, base_parent_world, chain_rl[key], target_tip,
                chain_lim[key], warm_start=warm.get(key), iterations=iters,
            )
            warm[key] = finger
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
    # Phase 3 (§7.5): default is a zero weapon offset -- the weapon stays where its
    # own animation puts it and the hands come to it. A non-zero offset is not yet
    # implemented (see progress.md), so reject it rather than silently ignore it.
    if cfg.get("weapon_offset") is not None:
        raise AssertionFailure("non-zero weapon_offset (Phase 3) is not implemented")
    classes = classify(scene, float(cfg["w_min"]))
    corr = correspond(scene, classes, bool(cfg.get("swap_arms", False)))
    dry_run = bool(job.get("dry_run", False))
    solved = {"frames": 0, "grip": {}} if dry_run else retarget(scene, corr, cfg)

    action = scene.src.animation_data.action
    frame_range = [int(action.frame_range[0]), int(action.frame_range[1])]
    return {
        "sequence": job["sequence"]["name"],
        "status": "MAPPED" if dry_run else "RETARGETED",
        "frames": solved["frames"],
        "grip": solved["grip"],
        "blender": bpy.app.version_string,
        "frame_range": frame_range,
        "counts": {
            "src_bones": len(scene.src.data.bones),
            "reference_bones": len(scene.reference.data.bones),
            "hand_bones": len(classes["hand"]),
            "weapon_bones": len(classes["weapon"]),
            "mapped_bones": sum(1 for m in corr.maps if m.source is not None),
            "held_tips": sum(1 for m in corr.maps if m.source is None),
        },
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
