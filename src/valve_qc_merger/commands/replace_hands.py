"""``replace-hands`` command: swap a weapon's hands for the reference hands.

Pipeline (Stage 1, FK retargeting):

1. Read the weapon QC to find its bodygroups. The non-hands bodygroup names the
   weapon reference SMD (the skeleton/bind source); the hands bodygroup is the
   one we replace.
2. Build the bone correspondence between the weapon rig and the reference hands
   and graft the reference hands onto the weapon skeleton.
3. Copy the weapon folder to the output, then:
   * write a grafted reference SMD (merged skeleton + reference mesh) per hand
     variant, copying its texture;
   * retarget every animation SMD in place (adding the grafted hand poses);
   * rewrite the QC hands bodygroup to point at the grafted SMDs.

The weapon geometry, its bones and every animation are preserved exactly, so
the gun keeps behaving as authored while the reference hands ride along.
"""

from __future__ import annotations

import argparse
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.correspondence import CorrespondenceError, build_hand_correspondences
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import Smd
from valve_qc_merger.parsers.smd import SmdParseError, parse_smd_file
from valve_qc_merger.qc_document import find_bodygroups, replace_bodygroup_studios
from valve_qc_merger.retarget import HandGraft
from valve_qc_merger.transform import Transform
from valve_qc_merger.writers.smd import write_smd_file

_DEFAULT_VARIANTS = ("male", "female")


def parse_offset(spec: str) -> Transform:
    """Parse ``rx,ry,rz,tx,ty,tz`` (rotation degrees, translation units)."""
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if len(parts) != 6:
        raise ValueError(f"offset must be 6 comma-separated numbers, got {spec!r}")
    try:
        values = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"offset values must be numbers: {spec!r}") from exc
    euler = Vector3(*(math.radians(v) for v in values[:3]))
    translation = Vector3(*values[3:])
    return Transform.from_pos_euler(translation, euler)


@dataclass(frozen=True, slots=True)
class ReplaceHandsResult:
    """Summary of a completed hand replacement."""

    output_dir: Path
    variants: tuple[str, ...]
    animations_retargeted: int
    weapon_bones: int
    output_bones: int
    removed_bones: int
    added_bones: int


class ReplaceHandsError(RuntimeError):
    """Raised when the weapon folder cannot be processed."""


def _find_qc(weapon_dir: Path) -> Path:
    qcs = sorted(weapon_dir.glob("*.qc"))
    if not qcs:
        raise ReplaceHandsError(f"no .qc file found in {weapon_dir}")
    if len(qcs) > 1:
        raise ReplaceHandsError(f"multiple .qc files in {weapon_dir}: {[q.name for q in qcs]}")
    return qcs[0]


def _resolve_studio(weapon_dir: Path, studio: str) -> Path:
    """Resolve a QC studio reference (relative, usually without extension) to a file."""
    candidate = weapon_dir / studio
    if candidate.suffix.lower() == ".smd" and candidate.exists():
        return candidate
    smd = weapon_dir / f"{studio}.smd"
    if smd.exists():
        return smd
    raise ReplaceHandsError(f"studio SMD not found for {studio!r} in {weapon_dir}")


def _weapon_reference(weapon_dir: Path, qc_text: str) -> Smd:
    for block in find_bodygroups(qc_text):
        if block.name.lower() == "hands" or not block.studios:
            continue
        return parse_smd_file(_resolve_studio(weapon_dir, block.studios[0]))
    raise ReplaceHandsError("QC has no non-hands bodygroup to source the weapon skeleton")


def _copy_textures(hand_smd: Smd, source_dir: Path, output_dir: Path) -> None:
    for material in hand_smd.materials():
        texture = source_dir / material
        if texture.exists():
            shutil.copy2(texture, output_dir / texture.name)


def _retarget_animations(graft: HandGraft, weapon_dir: Path, output_dir: Path) -> int:
    count = 0
    for smd_path in sorted(weapon_dir.rglob("*.smd")):
        try:
            smd = parse_smd_file(smd_path)
        except SmdParseError:
            continue
        if not smd.is_animation:
            continue
        destination = output_dir / smd_path.relative_to(weapon_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_smd_file(graft.retarget_animation(smd), destination)
        count += 1
    return count


def replace_hands(
    weapon_dir: Path,
    hands_dir: Path,
    output_dir: Path,
    variants: tuple[str, ...] = _DEFAULT_VARIANTS,
    offsets: dict[str, Transform] | None = None,
) -> ReplaceHandsResult:
    """Run the hand replacement and return a summary.

    ``offsets`` maps a hand side (``"L"``/``"R"``) to a constant alignment
    transform applied to that reference hand on the grip; the gun is compensated
    so it never moves. Omitted sides default to identity.
    """
    weapon_dir = weapon_dir.resolve()
    hands_dir = hands_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir == weapon_dir:
        raise ReplaceHandsError("output directory must differ from the weapon directory")

    qc_path = _find_qc(weapon_dir)
    qc_text = qc_path.read_text(encoding="latin-1")

    hands_block = next((b for b in find_bodygroups(qc_text) if b.name.lower() == "hands"), None)
    if hands_block is None:
        raise ReplaceHandsError("QC has no \"hands\" bodygroup to replace")

    available = [(name, hands_dir / f"{name}.smd") for name in variants]
    available = [(name, path) for name, path in available if path.exists()]
    if not available:
        raise ReplaceHandsError(f"no reference hand SMDs {variants} found in {hands_dir}")

    weapon_ref = _weapon_reference(weapon_dir, qc_text)
    canonical = parse_smd_file(available[0][1])
    try:
        links = build_hand_correspondences(weapon_ref, canonical)
    except CorrespondenceError as exc:
        raise ReplaceHandsError(f"could not match hands to weapon rig: {exc}") from exc
    canonical_graft = HandGraft(weapon_ref, canonical, links, offsets)

    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(weapon_dir, output_dir, dirs_exist_ok=True)

    new_studios: list[str] = []
    for name, smd_path in available:
        hand_smd = parse_smd_file(smd_path)
        graft = HandGraft(weapon_ref, hand_smd, links, offsets)
        studio = f"grafted_{name}"
        write_smd_file(graft.reference_smd(), output_dir / f"{studio}.smd")
        _copy_textures(hand_smd, hands_dir, output_dir)
        new_studios.append(studio)

    retargeted = _retarget_animations(canonical_graft, weapon_dir, output_dir)

    updated_qc = replace_bodygroup_studios(qc_text, hands_block, new_studios)
    (output_dir / qc_path.name).write_text(updated_qc, encoding="latin-1")

    output_bones = len(canonical_graft.merged_nodes())
    weapon_bones = len(weapon_ref.nodes)
    return ReplaceHandsResult(
        output_dir=output_dir,
        variants=tuple(name for name, _ in available),
        animations_retargeted=retargeted,
        weapon_bones=weapon_bones,
        output_bones=output_bones,
        removed_bones=canonical_graft.removed_count(),
        added_bones=canonical_graft.added_count(),
    )


class ReplaceHandsCommand(Command):
    """CLI wiring for :func:`replace_hands`."""

    name = "replace-hands"
    help = "Replace a weapon's view-model hands with the reference hands"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("weapon_dir", type=Path, help="folder holding the weapon QC and SMDs")
        parser.add_argument(
            "--hands",
            type=Path,
            required=True,
            metavar="DIR",
            help="folder holding the reference hand SMDs (male.smd, female.smd)",
        )
        parser.add_argument(
            "--output",
            type=Path,
            metavar="DIR",
            help="output folder (default: <weapon_dir>_rehanded)",
        )
        parser.add_argument(
            "--variants",
            default=",".join(_DEFAULT_VARIANTS),
            help="comma-separated reference hand names to graft (default: male,female)",
        )
        offset_help = (
            "alignment offset as 'rx,ry,rz,tx,ty,tz' (rotation degrees, translation "
            "units) applied to the %s hand on the grip; the gun stays put"
        )
        parser.add_argument("--left-offset", metavar="SPEC", help=offset_help % "left")
        parser.add_argument("--right-offset", metavar="SPEC", help=offset_help % "right")

    def run(self, args: argparse.Namespace) -> int:
        weapon_dir: Path = args.weapon_dir
        output_dir: Path = args.output or weapon_dir.with_name(f"{weapon_dir.name}_rehanded")
        variants = tuple(v.strip() for v in args.variants.split(",") if v.strip())
        try:
            offsets: dict[str, Transform] = {}
            if args.left_offset:
                offsets["L"] = parse_offset(args.left_offset)
            if args.right_offset:
                offsets["R"] = parse_offset(args.right_offset)
            result = replace_hands(weapon_dir, args.hands, output_dir, variants, offsets)
        except (ReplaceHandsError, SmdParseError, ValueError) as exc:
            print(f"replace-hands: {exc}")
            return 1

        print(f"Wrote rehanded weapon to {result.output_dir}")
        print(f"  variants grafted:      {', '.join(result.variants)}")
        print(
            f"  bones: {result.weapon_bones} -> {result.output_bones} "
            f"(removed {result.removed_bones} weapon hand bones, "
            f"added {result.added_bones} reference bones)"
        )
        print(f"  animations retargeted: {result.animations_retargeted}")
        return 0


__all__ = ["ReplaceHandsCommand", "ReplaceHandsResult", "replace_hands"]
