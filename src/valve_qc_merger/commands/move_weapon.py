"""``move-weapon``: slide the weapon of an already-built model into the grip.

``replace-hands`` assembles the model with your hands and clean geometry, but on
some rigs the weapon does not sit perfectly in the grip. Rather than guess the
distance up front, you open the build in a modeller, see the weapon in your
hands, measure how far it needs to move, and feed that here -- this bakes the
move into the already-built model without reassembling it.

The move is a *world* translation in the same units and axes a modeller shows
(what you'd type as the weapon's Location), baked per gun bone exactly as
``replace-hands`` bakes ``--weapon-offset``, so it rides every animation. Only the
weapon meshes move; the hands and animations are untouched. It is *cumulative* --
each call slides the weapon from where it currently sits -- so you can nudge it
into place in the steps you measured.
"""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.commands.replace_hands import (
    ReplaceHandsError,
    _bake_world_offset,
    _find_qc,
    _weapon_studio_paths,
    parse_translation,
)
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.parsers.smd import SmdParseError, parse_smd_file
from valve_qc_merger.writers.smd import write_smd_file


@dataclass(frozen=True, slots=True)
class MoveWeaponResult:
    """Summary of a completed weapon move."""

    output_dir: Path
    offset: Vector3
    studios_moved: int


def move_weapon(
    model_dir: Path, offset: Vector3, output_dir: Path | None = None
) -> MoveWeaponResult:
    """Slide the weapon meshes of a built model by ``offset`` (modeller units).

    ``model_dir`` is a folder produced by ``replace-hands``. Every non-hands
    (weapon) bodygroup studio is slid by ``offset`` using the same per-bone world
    bake as ``replace-hands``, so the move rides all animations; the grafted hands
    and the animation SMDs are left exactly as they were. With no ``output_dir``
    the model is edited in place (so repeated calls accumulate); give one to write
    a moved copy and leave the original build untouched.
    """
    if output_dir is not None and output_dir != model_dir:
        shutil.copytree(model_dir, output_dir, dirs_exist_ok=True)
        target = output_dir
    else:
        target = model_dir

    qc_path = _find_qc(target)
    qc_text = qc_path.read_text(encoding="latin-1")
    studios = _weapon_studio_paths(target, qc_text)
    for studio_path in studios:
        gun = parse_smd_file(studio_path)
        write_smd_file(_bake_world_offset(gun, offset), studio_path)
    return MoveWeaponResult(output_dir=target, offset=offset, studios_moved=len(studios))


class MoveWeaponCommand(Command):
    """CLI wiring for :func:`move_weapon`."""

    name = "move-weapon"
    help = "Slide the weapon of a built model into the grip (measured in a modeller)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "model_dir",
            type=Path,
            help="a model folder produced by replace-hands",
        )
        parser.add_argument(
            "--by",
            required=True,
            metavar="X,Y,Z",
            help="move the weapon by X,Y,Z units (the same Location you measured in "
            "your modeller); cumulative across calls",
        )
        parser.add_argument(
            "--output",
            type=Path,
            metavar="DIR",
            help="write a moved copy here instead of editing the build in place",
        )

    def run(self, args: argparse.Namespace) -> int:
        try:
            offset = parse_translation(args.by)
            result = move_weapon(args.model_dir, offset, args.output)
        except (ReplaceHandsError, SmdParseError, ValueError) as exc:
            print(f"move-weapon: {exc}")
            return 1

        o = result.offset
        print(f"Moved weapon by ({o.x:.2f}, {o.y:.2f}, {o.z:.2f}) in {result.output_dir}")
        print(f"  weapon studios moved:  {result.studios_moved} (hands and animations untouched)")
        return 0


__all__ = ["MoveWeaponCommand", "MoveWeaponResult", "move_weapon"]
