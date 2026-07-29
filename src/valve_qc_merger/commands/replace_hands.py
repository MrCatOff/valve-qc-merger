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

from valve_qc_merger.clearance import (
    away_direction,
    gun_vertices_inside_hand,
    palm_seat_offset,
    weapon_clearance_offset,
)
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
_NO_WEAPON_OFFSET = Vector3(0.0, 0.0, 0.0)


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


def parse_translation(spec: str) -> Vector3:
    """Parse ``x,y,z`` translation units."""
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if len(parts) != 3:
        raise ValueError(f"offset must be 3 comma-separated numbers, got {spec!r}")
    try:
        return Vector3(*(float(part) for part in parts))
    except ValueError as exc:
        raise ValueError(f"offset values must be numbers: {spec!r}") from exc


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
    weapon_slide: Vector3 | None = None
    intrusion_before: int = 0
    intrusion_after: int = 0


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


def _weapon_studio_paths(weapon_dir: Path, qc_text: str) -> list[Path]:
    """Resolve the SMD files of every non-hands (weapon) bodygroup studio."""
    paths: list[Path] = []
    for block in find_bodygroups(qc_text):
        if block.name.lower() == "hands":
            continue
        for studio in block.studios:
            paths.append(_resolve_studio(weapon_dir, studio))
    if not paths:
        raise ReplaceHandsError("QC has no non-hands bodygroup to source the weapon skeleton")
    return paths


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
    weapon_offset: Vector3 = _NO_WEAPON_OFFSET,
    clearance: bool = False,
    clearance_direction: Vector3 | None = None,
    seat_grip: bool = True,
    finger_ik: bool = True,
) -> ReplaceHandsResult:
    """Run the hand replacement and return a summary.

    ``offsets`` maps a hand side (``"L"``/``"R"``) to a constant alignment
    transform applied to that reference hand on the grip; the gun is compensated
    so it never moves. Omitted sides default to identity. ``weapon_offset``
    translates the gun geometry off the hands so they do not clip the grip.

    When ``clearance`` is set, the gun is slid until the reference hands overlap
    it no more than the *original* weapon hands did (plus a small margin), along
    ``clearance_direction`` if given, otherwise automatically away from the hand.
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

    weapon_studios = _weapon_studio_paths(weapon_dir, qc_text)
    weapon_ref = parse_smd_file(weapon_studios[0])
    canonical = parse_smd_file(available[0][1])
    try:
        links = build_hand_correspondences(weapon_ref, canonical)
    except CorrespondenceError as exc:
        raise ReplaceHandsError(f"could not match hands to weapon rig: {exc}") from exc
    canonical_graft = HandGraft(weapon_ref, canonical, links, offsets, weapon_offset, finger_ik)

    weapon_slide: Vector3 | None = None
    intrusion_before = intrusion_after = 0
    if seat_grip:
        original_hand = parse_smd_file(_resolve_studio(weapon_dir, hands_block.studios[0]))
        finger_bones = {
            joint
            for link in links
            for source, _ in link.finger_pairs
            for joint in source.joints
        }
        seat = palm_seat_offset(
            canonical_graft.reference_smd(),
            original_hand,
            canonical_graft.weapon_reference_smd(),
            {"Bip01_R_Hand", "Bip01_L_Hand"},
            finger_bones,
        )
        weapon_slide = seat
        weapon_offset = Vector3(
            weapon_offset.x + seat.x, weapon_offset.y + seat.y, weapon_offset.z + seat.z
        )
        canonical_graft = HandGraft(weapon_ref, canonical, links, offsets, weapon_offset, finger_ik)
    if clearance or clearance_direction is not None:
        our_hand = canonical_graft.reference_smd()
        gun_ref = canonical_graft.weapon_reference_smd()
        # The original weapon hands set the acceptable overlap level.
        original_hand = parse_smd_file(_resolve_studio(weapon_dir, hands_block.studios[0]))
        baseline = gun_vertices_inside_hand(original_hand, gun_ref)
        direction = (
            clearance_direction
            if clearance_direction is not None
            else away_direction(our_hand, gun_ref)
        )
        slide, intrusion_before, intrusion_after = weapon_clearance_offset(
            our_hand, gun_ref, direction, target_inside=baseline
        )
        weapon_slide = slide
        weapon_offset = Vector3(
            weapon_offset.x + slide.x, weapon_offset.y + slide.y, weapon_offset.z + slide.z
        )
        canonical_graft = HandGraft(weapon_ref, canonical, links, offsets, weapon_offset, finger_ik)

    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(weapon_dir, output_dir, dirs_exist_ok=True)

    # Drop the weapon's original hand-mesh SMDs: they are no longer referenced
    # and still carry the old hand bones, which would re-introduce the conflict.
    for studio in hands_block.studios:
        stale = output_dir / _resolve_studio(weapon_dir, studio).relative_to(weapon_dir)
        stale.unlink(missing_ok=True)

    new_studios: list[str] = []
    for name, smd_path in available:
        hand_smd = parse_smd_file(smd_path)
        graft = HandGraft(weapon_ref, hand_smd, links, offsets, finger_ik=finger_ik)
        studio = f"grafted_{name}"
        write_smd_file(graft.reference_smd(), output_dir / f"{studio}.smd")
        _copy_textures(hand_smd, hands_dir, output_dir)
        new_studios.append(studio)

    # Re-express every weapon reference on the merged skeleton so all SMDs share
    # one bone set (the untouched originals still declare the old hand bones,
    # which would conflict with the grafted hand bones when the model is built).
    for studio_path in weapon_studios:
        source = weapon_ref if studio_path == weapon_studios[0] else parse_smd_file(studio_path)
        remapped = canonical_graft.weapon_reference_smd(source)
        write_smd_file(remapped, output_dir / studio_path.relative_to(weapon_dir))

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
        weapon_slide=weapon_slide,
        intrusion_before=intrusion_before,
        intrusion_after=intrusion_after,
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
        parser.add_argument(
            "--weapon-offset",
            metavar="X,Y,Z",
            help="translate the gun geometry by X,Y,Z units so the hands do not "
            "clip the grip",
        )
        parser.add_argument(
            "--weapon-clearance",
            metavar="auto|X,Y,Z",
            help="slide the gun until the reference hands overlap it no more than "
            "the original hands did. Use 'auto' to also pick the slide direction "
            "(away from the hand), or give a grip-preserving direction X,Y,Z",
        )
        parser.add_argument(
            "--seat-grip",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="position the gun so the reference palm holds the grip where the "
            "original hands' palm did (automatic, per weapon; on by default, "
            "disable with --no-seat-grip)",
        )
        parser.add_argument(
            "--finger-ik",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="curl each finger so its tip reaches the weapon fingertip (the grip "
            "contact point) instead of pointing straight and overshooting (on by "
            "default, disable with --no-finger-ik)",
        )

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
            weapon_offset = (
                parse_translation(args.weapon_offset)
                if args.weapon_offset
                else _NO_WEAPON_OFFSET
            )
            clearance = bool(args.weapon_clearance)
            clearance_direction = (
                None
                if not args.weapon_clearance or args.weapon_clearance == "auto"
                else parse_translation(args.weapon_clearance)
            )
            result = replace_hands(
                weapon_dir,
                args.hands,
                output_dir,
                variants,
                offsets,
                weapon_offset,
                clearance,
                clearance_direction,
                args.seat_grip,
                args.finger_ik,
            )
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
        if result.weapon_slide is not None:
            s = result.weapon_slide
            detail = (
                f"[hand intrusion {result.intrusion_before} -> {result.intrusion_after} verts]"
                if result.intrusion_before
                else "[grip seated in palm]"
            )
            print(f"  weapon moved:          ({s.x:.2f}, {s.y:.2f}, {s.z:.2f}) {detail}")
        return 0


__all__ = ["ReplaceHandsCommand", "ReplaceHandsResult", "replace_hands"]
