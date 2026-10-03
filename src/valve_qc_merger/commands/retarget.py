"""CLI wiring for the ``retarget`` command (handswap engine).

Replaces the original hands of a decompiled CS 1.6 ``v_`` model with the CSO
hands, retargets every animation onto them, rewrites the QC and (optionally)
compiles the ``.mdl`` — all pure numpy on SMD/QC text, no Blender in the
per-weapon path. The work lives in :mod:`valve_qc_merger.services.retarget`;
this module only declares the flags.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.services.base import options_from
from valve_qc_merger.services.retarget import (
    RetargetOptions,
    resolve_out_dir,
    run_retarget,
)


class RetargetCommand(Command):
    """Swap a weapon's original hands for the CSO hands and retarget its
    animations onto them (pure-Python handswap engine)."""

    name = "retarget"
    help = ("replace a decompiled viewmodel's hands with the CSO hands and "
            "retarget every animation onto them (pure Python, no Blender)")

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--weapon-dir", type=Path, required=True,
                            help="decompiled weapon folder (QC + reference "
                                 "SMDs + <name>_anims/*.smd + textures)")
        parser.add_argument("--category", default="uncategorized",
                            help="destination bucket under storage/retarget/ "
                                 "(default: uncategorized); the model lands in "
                                 "storage/retarget/{category}/{model}")
        parser.add_argument("--out", type=Path,
                            help="output directory; overrides the default "
                                 "storage/retarget/{category}/{model} location")
        parser.add_argument("--qc", type=Path,
                            help="QC file (default: the only .qc in weapon-dir)")
        parser.add_argument("--asset", type=Path,
                            help="CSO hands asset (default: "
                                 "storage/handswap/cso_hands.json.gz)")
        parser.add_argument("--hands-texture", type=Path,
                            help="BMP copied to <out>/hands.bmp "
                                 "(default: storage/hands/male.bmp)")
        parser.add_argument("--modelname", help="override $modelname")
        parser.add_argument("--studiomdl", type=Path,
                            help="studiomdl binary (default: tmp/studiomdl)")
        parser.add_argument("--compile", action="store_true",
                            help="run studiomdl after verifying")
        parser.add_argument("--no-verify", dest="verify", action="store_false",
                            default=True,
                            help="skip the output-file verification suite")
        # grip tuning (see storage/handswap/grip_tuning.json)
        parser.add_argument("--no-snug", dest="snug", action="store_false",
                            default=True,
                            help="disable the automatic finger snug-to-weapon "
                                 "curl (on by default)")
        parser.add_argument("--snug-max-deg", type=float, default=None,
                            help="per-joint clamp for the automatic snug curl "
                                 "(default: the weapon's grip tuning, else 35)")
        parser.add_argument("--curl", action="append", default=[],
                            metavar="side:finger:deg",
                            help="extra curl per joint of one finger, + closes, - "
                                 "opens (either hand), e.g. "
                                 "'left:ForeFinger:+8' (repeatable)")
        parser.add_argument("--grip-offset", action="append", default=[],
                            metavar="side:dx,dy,dz",
                            help="shift the palm relative to the weapon in "
                                 "palm axes (X fingers-forward, Y toward "
                                 "thumb, Z palm normal), e.g. "
                                 "'left:0,0,-0.4' (repeatable)")
        parser.add_argument("--weapon-offset", type=_xyz, default=[],
                            metavar="dx,dy,dz",
                            help="move the weapon relative to both hands, in "
                                 "model space at the grip frame, e.g. "
                                 "'0,0.5,0' (then fingers re-snug); overrides "
                                 "the weapon's weapon_offset in grip_tuning.json")

    def run(self, args: argparse.Namespace) -> int:
        return run_retarget(options_from(RetargetOptions, args)).exit_code

def _xyz(text: str) -> list[float]:
    try:
        values = [float(part) for part in text.split(",")]
    except ValueError:
        values = []
    if len(values) != 3:
        raise argparse.ArgumentTypeError(f"expected dx,dy,dz, got {text!r}")
    return values


def _resolve_out_dir(args: argparse.Namespace) -> Path:
    """Where ``retarget`` writes for these CLI arguments (see the service)."""
    return resolve_out_dir(options_from(RetargetOptions, args))


__all__ = ["RetargetCommand"]
