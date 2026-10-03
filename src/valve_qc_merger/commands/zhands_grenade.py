"""CLI wiring for ``zhands-grenade``: give zombie hands a grenade.

The work lives in :mod:`valve_qc_merger.services.zhands_grenade`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.commands.retarget import _xyz
from valve_qc_merger.services.base import options_from
from valve_qc_merger.services.zhands_grenade import (
    ZhandsGrenadeOptions,
    grenade_name,
    run_zhands_grenade,
)


class ZhandsGrenadeCommand(Command):
    """Retarget a grenade donor's animation onto a zombie's hands."""

    name = "zhands-grenade"
    help = ("make v_<zombie>_grenade from a zombie's knife model: the donor's "
            "frog bomb and hand animation (bundled banshee by default) on these hands")

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--knife-dir", type=Path, required=True,
                            help="decompiled zombie knife model whose hands get the grenade")
        parser.add_argument("--out", type=Path,
                            help="output folder (default: v_<zombie>_grenade next to the knife)")
        parser.add_argument("--donor", type=Path,
                            help="decompiled grenade model to take the frog bomb and the "
                                 "animation from (default: the bundled "
                                 "storage/zhands/grenade_donor/v_banshee_grenade)")
        parser.add_argument("--modelname", help="override $modelname")
        parser.add_argument("--snug-max-deg", type=float, default=None,
                            help="per-joint clamp of the automatic finger fit")
        parser.add_argument("--curl", action="append", default=[], metavar="side:finger:deg",
                            help="extra curl per joint of one finger, + closes (repeatable)")
        parser.add_argument("--grip-offset", action="append", default=[],
                            metavar="side:dx,dy,dz",
                            help="shift one palm in palm axes (repeatable)")
        parser.add_argument("--weapon-offset", type=_xyz, default=[], metavar="dx,dy,dz",
                            help="move the frog bomb relative to both hands")

    def run(self, args: argparse.Namespace) -> int:
        if args.out is None:
            args.out = args.knife_dir.parent / grenade_name(args.knife_dir.name)
        return run_zhands_grenade(options_from(ZhandsGrenadeOptions, args)).exit_code


__all__ = ["ZhandsGrenadeCommand"]
