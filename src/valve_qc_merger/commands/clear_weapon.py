"""``clear-weapon``: slide the weapon out of the hands to clear the grip.

The reference hands are a different length than the weapon's original hands, so
the grip -- placed for the originals -- can push the gun *into* the finger mesh.
The fix is the one a modeller would do by eye: slide the weapon along the grip
until the hands stop overlapping it. This command finds that slide automatically.

It solves against the **idle** pose (what the player sees), using Blender's fast
BVH overlap in headless mode (``blender -b -P``): the weapon is nudged in small
steps until the hand-vs-gun triangle intersections stop dropping, then that
offset is baked into the weapon meshes (the same per-bone world bake as
``move-weapon``/``replace-hands``), so it rides every animation. Only the weapon
moves; the hands and animations are untouched. It is cumulative and can be
followed by ``move-weapon`` for any manual fine-tuning.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.commands.replace_hands import (
    ReplaceHandsError,
    _bake_world_offset,
    _find_qc,
    _resolve_studio,
    _try_parse,
    _weapon_studio_paths,
)
from valve_qc_merger.kinematics import world_transforms
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.qc_document import find_bodygroups
from valve_qc_merger.writers.smd import write_smd_file

# Blender script: build the hand and gun at the idle pose, then greedily slide the
# gun to minimise hand-vs-gun triangle overlap. Prints ``CLEARANCE {json}``.
_BLENDER_SOLVER = r"""
import bpy, sys, json, mathutils
from mathutils.bvhtree import BVHTree
a = sys.argv[sys.argv.index("--") + 1:]
src, hand_smd, gun_smd, idle_smd, frame = a[0], a[1], a[2], a[3], int(a[4])
sys.path.insert(0, src)
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.kinematics import world_transforms
gun = parse_smd_file(gun_smd); hand = parse_smd_file(hand_smd); anim = parse_smd_file(idle_smd)
ref = world_transforms(gun.nodes, gun.frames[0])
gnm = {n.index: n.name for n in gun.nodes}; hnm = {n.index: n.name for n in hand.nodes}
fr = min(anim.frames, key=lambda f: abs(f.time - frame))
aw = world_transforms(anim.nodes, fr); ai = {n.name: n.index for n in anim.nodes}
def deform(smd, names):
    out = []
    for t in smd.triangles:
        tri = []
        for v in t.vertices:
            loc = ref[v.bone].inverse().transform_point(v.position)
            p = aw[ai[names[v.bone]]].transform_point(loc); tri.append((p.x, p.y, p.z))
        out.append(tri)
    return out
hand_tris = deform(hand, hnm); gun_tris = deform(gun, gnm)
def bvh(tris, off=(0, 0, 0)):
    verts = []; polys = []
    for tri in tris:
        b = len(verts)
        for p in tri:
            verts.append(mathutils.Vector((p[0] + off[0], p[1] + off[1], p[2] + off[2])))
        polys.append((b, b + 1, b + 2))
    return BVHTree.FromPolygons(verts, polys)
hb = bvh(hand_tris)
overlap = lambda off: len(hb.overlap(bvh(gun_tris, off)))
base = overlap((0, 0, 0)); off = [0.0, 0.0, 0.0]; cur = base
for _ in range(60):
    best = (cur, None)
    for ax in range(3):
        for s in (0.25, -0.25):
            t = list(off); t[ax] += s; val = overlap(tuple(t))
            if val < best[0]:
                best = (val, tuple(t))
    if best[1] is None or best[0] == 0:
        if best[1] is not None:
            off = list(best[1]); cur = best[0]
        break
    off = list(best[1]); cur = best[0]
print("CLEARANCE " + json.dumps({"offset": off, "before": base, "after": cur}), flush=True)
"""


@dataclass(frozen=True, slots=True)
class ClearWeaponResult:
    """Summary of a completed clearance."""

    output_dir: Path
    offset: Vector3
    overlap_before: int
    overlap_after: int


def _find_blender() -> str:
    """Locate the Blender executable (``$BLENDER``, PATH, or a default install)."""
    for candidate in (os.environ.get("BLENDER"), shutil.which("blender")):
        if candidate and Path(candidate).exists():
            return candidate
    for path in (
        "/Applications/Blender.app/Contents/MacOS/Blender",
        "/usr/bin/blender",
        "/snap/bin/blender",
        r"C:\Program Files\Blender Foundation\Blender\blender.exe",
    ):
        if Path(path).exists():
            return path
    raise ReplaceHandsError(
        "Blender not found; set the BLENDER environment variable to its executable"
    )


def _idle_animation(model_dir: Path) -> Path:
    """The idle animation SMD (preferred), else any animation."""
    animations = [
        p
        for p in sorted(model_dir.rglob("*.smd"))
        if (smd := _try_parse(p)) is not None and smd.is_animation
    ]
    if not animations:
        raise ReplaceHandsError(f"no animation SMDs found under {model_dir}")
    return next((p for p in animations if "idle" in p.stem.lower()), animations[0])


def _hand_studio(model_dir: Path, qc_text: str) -> Path:
    """The grafted reference-hand mesh (male preferred)."""
    hands = next((b for b in find_bodygroups(qc_text) if b.name.lower() == "hands"), None)
    studios = list(hands.studios) if hands else []
    male = next((s for s in studios if "female" not in s.lower()), studios[0] if studios else None)
    if male is None:
        raise ReplaceHandsError("QC has no hands bodygroup to read the reference hand from")
    return _resolve_studio(model_dir, male)


def _solve_offset(model_dir: Path, gun: Path, hand: Path, idle: Path) -> tuple[Vector3, int, int]:
    """Run Blender to find the idle-pose slide, and rotate it into the reference frame."""
    src = str(Path(__file__).resolve().parents[2])
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as script:
        script.write(_BLENDER_SOLVER)
        script_path = script.name
    try:
        command = [_find_blender(), "-b", "-P", script_path, "--"]
        command += [src, str(hand), str(gun), str(idle), "0"]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=600)
    finally:
        os.unlink(script_path)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("CLEARANCE ")), None)
    if line is None:
        raise ReplaceHandsError(f"Blender clearance solve failed:\n{proc.stdout[-2000:]}")
    data = json.loads(line[len("CLEARANCE ") :])
    off_idle = Vector3(*data["offset"])

    # The slide was found at the idle pose; express it in the reference frame (so
    # the per-bone bake reproduces it) via the largest gun bone's pose rotation.
    gun_smd = parse_smd_file(gun)
    idle_smd = parse_smd_file(idle)
    from collections import Counter

    grip = Counter(v.bone for t in gun_smd.triangles for v in t.vertices).most_common(1)[0][0]
    names = {n.index: n.name for n in gun_smd.nodes}
    idle_index = {n.name: n.index for n in idle_smd.nodes}
    r_ref = world_transforms(gun_smd.nodes, gun_smd.frames[0])[grip]
    r_idle = world_transforms(idle_smd.nodes, idle_smd.frames[0])[idle_index[names[grip]]]
    off_ref = r_ref.rotate_vector(r_idle.inverse().rotate_vector(off_idle))
    return off_ref, int(data["before"]), int(data["after"])


def clear_weapon(model_dir: Path, output_dir: Path | None = None) -> ClearWeaponResult:
    """Slide the weapon of a built model out of the hands to clear the grip."""
    if output_dir is not None and output_dir != model_dir:
        shutil.copytree(model_dir, output_dir, dirs_exist_ok=True)
        target = output_dir
    else:
        target = model_dir

    qc_text = _find_qc(target).read_text(encoding="latin-1")
    studios = _weapon_studio_paths(target, qc_text)
    offset, before, after = _solve_offset(
        target, studios[0], _hand_studio(target, qc_text), _idle_animation(target)
    )
    for studio_path in studios:
        gun = parse_smd_file(studio_path)
        write_smd_file(_bake_world_offset(gun, offset), studio_path)
    return ClearWeaponResult(target, offset, before, after)


class ClearWeaponCommand(Command):
    """CLI wiring for :func:`clear_weapon`."""

    name = "clear-weapon"
    help = "Auto-slide a built weapon out of the hands so the grip stops clipping"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("model_dir", type=Path, help="a model folder produced by replace-hands")
        parser.add_argument(
            "--output",
            type=Path,
            metavar="DIR",
            help="write a cleared copy here instead of editing the build in place",
        )

    def run(self, args: argparse.Namespace) -> int:
        try:
            result = clear_weapon(args.model_dir, args.output)
        except (ReplaceHandsError, ValueError, subprocess.SubprocessError) as exc:
            print(f"clear-weapon: {exc}", file=sys.stderr)
            return 1
        o = result.offset
        print(f"Cleared weapon in {result.output_dir}")
        print(f"  slid weapon by:      ({o.x:.2f}, {o.y:.2f}, {o.z:.2f})")
        print(
            f"  idle grip overlap:   {result.overlap_before} -> "
            f"{result.overlap_after} triangles"
        )
        return 0


__all__ = ["ClearWeaponCommand", "ClearWeaponResult", "clear_weapon"]
