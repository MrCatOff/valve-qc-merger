"""CLI wiring for the ``retarget`` command (handswap engine).

Replaces the original hands of a decompiled CS 1.6 ``v_`` model with the CSO
hands, retargets every animation onto them, rewrites the QC and (optionally)
compiles the ``.mdl`` — all pure numpy on SMD/QC text, no Blender in the
per-weapon path. The heavy lifting lives in
:mod:`valve_qc_merger.handswap`; this command only resolves inputs/outputs,
maps the model into ``storage/retarget/{category}/{model}`` for the merge
pipeline, and turns the engine's outcome into an exit code.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

from valve_qc_merger.commands.base import Command
from valve_qc_merger.resources import resource_path

# Exit codes (kept compatible with the previous retarget command).
EXIT_OK = 0
EXIT_FAIL = 2       # conversion ran but verification/compile failed
EXIT_DISCOVERY = 3  # bad or missing inputs (no QC, no hands, missing files)


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
        parser.add_argument("--snug-max-deg", type=float, default=18.0,
                            help="per-joint clamp for the automatic snug curl")
        parser.add_argument("--curl", action="append", default=[],
                            metavar="side:finger:deg",
                            help="manual extra finger curl, e.g. "
                                 "'left:ForeFinger:+8' (repeatable)")
        parser.add_argument("--grip-offset", action="append", default=[],
                            metavar="side:dx,dy,dz",
                            help="shift the palm relative to the weapon in "
                                 "palm axes (X fingers-forward, Y toward "
                                 "thumb, Z palm normal), e.g. "
                                 "'left:0,0,-0.4' (repeatable)")
        parser.add_argument("--arm-dir", action="append", default=[],
                            metavar="side:x,y,z",
                            help="pin the forearm/elbow direction (model space) "
                                 "toward the player, for weapons whose arm would "
                                 "otherwise reach into the barrel, e.g. "
                                 "'left:0,-8,-6' (repeatable)")

    def run(self, args: argparse.Namespace) -> int:
        from valve_qc_merger.handswap import convert as convertmod

        weapon_dir = args.weapon_dir
        if not weapon_dir.is_dir():
            print(f"error: weapon dir not found: {weapon_dir}")
            return EXIT_DISCOVERY

        try:
            out_dir = _resolve_out_dir(args)
        except ValueError as exc:
            print(f"error: {exc}")
            return EXIT_DISCOVERY

        # Build the namespace handswap.convert.convert() expects; leaving an
        # optional at None lets the engine fall back to its own default.
        conv_args = SimpleNamespace(
            weapon_dir=str(weapon_dir),
            out=str(out_dir),
            qc=(str(args.qc) if args.qc else None),
            asset=(str(resource_path(args.asset)) if args.asset
                   else convertmod.assetmod.DEFAULT_ASSET),
            hands_texture=(str(resource_path(args.hands_texture))
                           if args.hands_texture else None),
            modelname=args.modelname,
            studiomdl=(str(resource_path(args.studiomdl)) if args.studiomdl
                       else convertmod.DEFAULT_STUDIOMDL),
            compile=args.compile,
            verify=args.verify,
            snug=args.snug,
            snug_max_deg=args.snug_max_deg,
            curl=args.curl,
            grip_offset=args.grip_offset,
            arm_dir=args.arm_dir,
        )

        print(f"  output: {out_dir}")
        try:
            info = convertmod.convert(conv_args)
        except FileNotFoundError as exc:
            print(f"error: {exc}")
            return EXIT_DISCOVERY
        except RuntimeError as exc:
            # no hands found => discovery; verify/compile failure => fail
            message = str(exc)
            print(f"error: {message}")
            lowered = message.lower()
            if "verification" in lowered or "studiomdl" in lowered:
                return EXIT_FAIL
            return EXIT_DISCOVERY

        report = info.get("verify")
        if report is not None and not report.get("ok", True):
            return EXIT_FAIL
        return EXIT_OK


def _resolve_out_dir(args: argparse.Namespace) -> Path:
    """An explicit ``--out`` wins; otherwise the model lands under
    ``storage/retarget/{category}/{model}`` (model = weapon-dir name), the
    layout the merge commands consume."""
    if args.out is not None:
        return args.out
    category = args.category.strip()
    if not category or "/" in category or "\\" in category or \
            category in {".", ".."}:
        raise ValueError(f"invalid --category {args.category!r} "
                         "(must be a plain name)")
    model = args.weapon_dir.name or args.weapon_dir.resolve().name
    return resource_path(Path("storage") / "retarget" / category / model)


__all__ = ["RetargetCommand"]
