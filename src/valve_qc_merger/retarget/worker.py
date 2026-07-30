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
import sys
import traceback
from typing import Any

import addon_utils  # type: ignore[import-not-found]
import bpy  # type: ignore[import-not-found]

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
    """Enable Blender Source Tools and assert its scale is 1.0 (§3, §7.1)."""
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
    before: set[str] = set()

    _import_smd(job["weapon_pv"], "NEW_ARMATURE")
    src = _only_new_armature(before)
    src.name = "SRC"
    weapon_mesh = _newest_mesh_for(src)

    # Attach the sequence animation onto the matching weapon rig (identical bones).
    _import_smd(job["sequence"]["path"], "APPEND")
    if not (src.animation_data and src.animation_data.action):
        raise AssertionFailure("animation did not attach to the source rig")

    before = {a.name for a in _armatures()}
    _import_smd(job["original_hands"], "NEW_ARMATURE")
    original_rig = _only_new_armature(before)
    original_mesh = _newest_mesh_for(original_rig)
    _rebind(original_mesh, src)  # pose the original hand by the animation
    bpy.data.objects.remove(original_rig, do_unlink=True)

    before = {a.name for a in _armatures()}
    _import_smd(job["reference"], "NEW_ARMATURE")
    reference = _only_new_armature(before)
    reference_mesh = _newest_mesh_for(reference)

    return Scene(src, reference, weapon_mesh, original_mesh, reference_mesh)


def _newest_mesh_for(armature: Any) -> Any:
    """The mesh bound to ``armature`` by an Armature modifier."""
    bound = [
        o for o in bpy.data.objects
        if o.type == "MESH"
        and any(m.type == "ARMATURE" and m.object == armature for m in o.modifiers)
    ]
    if not bound:
        raise AssertionFailure(f"no mesh bound to {armature.name}")
    # BST imports one mesh per reference SMD; take the most recently added.
    return bound[-1]


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
# Entry point
# --------------------------------------------------------------------------- #
def run(job: dict[str, Any]) -> dict[str, Any]:
    cfg = job["config"]
    enable_bst()
    clean_scene(float(cfg["fps"]))
    scene = import_scene(job)
    classes = classify(scene, float(cfg["w_min"]))

    action = scene.src.animation_data.action
    frame_range = [int(action.frame_range[0]), int(action.frame_range[1])]
    return {
        "sequence": job["sequence"]["name"],
        "status": "IMPORTED",
        "blender": bpy.app.version_string,
        "frame_range": frame_range,
        "counts": {
            "src_bones": len(scene.src.data.bones),
            "reference_bones": len(scene.reference.data.bones),
            "hand_bones": len(classes["hand"]),
            "weapon_bones": len(classes["weapon"]),
        },
        "classification": classes,
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
    except DiscoveryFailure as exc:
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
