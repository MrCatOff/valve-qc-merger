"""CLI wiring for merge-v (docs/merge-v.md).

The work lives in :mod:`valve_qc_merger.services.merge_view`; this module
only declares the flags, applies an optional TOML config and prints.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.merge_view.parts import (
    SEQUENCE_BUDGET,
    SHARED_HANDS_SEQUENCE_BUDGET,
    TEXTURE_BUDGET,
)
from valve_qc_merger.retarget.config import (
    DEFAULT_REFERENCE,
    DEFAULT_SHARED_HANDS_REFERENCE,
)
from valve_qc_merger.services.base import options_from
from valve_qc_merger.services.merge_view import MergeViewOptions, run_merge_view


class MergeViewCommand(Command):
    """Merge decompiled view-models onto one canonical hand skeleton."""

    name = "merge-v"
    help = "merge a folder of decompiled view-models into combined models (spec draft)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("models_dir", type=Path,
                            help="parent directory; each subdir with one .qc is a model")
        parser.add_argument("--out", type=Path, required=True, help="output directory")
        parser.add_argument("--name", default="v_merged", help="output model name stem")
        parser.add_argument("--exclude", action="append", default=[],
                            metavar="NAME", help="skip a model directory (repeatable)")
        parser.add_argument("--reference", type=Path, default=None,
                            help="canonical hand skeleton SMD (default: "
                                 f"{DEFAULT_REFERENCE}; with --shared-hands the "
                                 f"CSO hands {DEFAULT_SHARED_HANDS_REFERENCE}, "
                                 "which carry the full arm so the elbow is kept)")
        parser.add_argument("--skip-unmatched", action="store_true",
                            help="continue past models whose rig cannot be matched")
        parser.add_argument("--shared-hands", action="store_true",
                            help="all inputs already wear our male/female hands "
                                 "(e.g. the retarget output): emit ONE shared hands "
                                 "bodygroup instead of per-weapon hands, so pev_body "
                                 "stays hand+weapon*2 (<255). Multi-part weapons "
                                 "(>1 weapon submodel) are rejected — use them alone")
        parser.add_argument("--prune", action="store_true",
                            help="also fold away vertex-less unreferenced bones "
                                 "(default keeps everything except Finger*Nub)")
        parser.add_argument("--no-pool-bones", action="store_true",
                            help="skip bone pooling (merged table may exceed 127)")
        parser.add_argument("--manifest-format", choices=("ini", "json", "toml"),
                            default="ini", help="per-model manifest format")
        parser.add_argument("--texture-budget", type=int, default=TEXTURE_BUDGET,
                            help="max textures per compiled part (default "
                                 f"{TEXTURE_BUDGET}; hard engine cap is 100)")
        parser.add_argument("--sequence-budget", type=int, default=None,
                            help="max sequences per compiled part after "
                                 f"dedupe (default {SEQUENCE_BUDGET}; "
                                 f"{SHARED_HANDS_SEQUENCE_BUDGET} with "
                                 "--shared-hands)")
        parser.add_argument("--max-texture-size", type=int, metavar="N",
                            help="downscale staged textures larger than N on "
                                 "either axis (8-bit re-quantised)")
        parser.add_argument("--pack-textures", action="store_true",
                            help="pack eligible textures four-to-a-file into "
                                 "512x512 atlases (shared palette, UVs "
                                 "rewritten; chrome/additive/tiling excluded)")
        parser.add_argument("--no-pack-texture", action="append", default=[],
                            metavar="GLOB",
                            help="keep matching textures out of atlases "
                                 "(repeatable)")
        parser.add_argument("--sound-path", metavar="TEMPLATE",
                            help="rewrite sound event paths for every weapon; "
                                 "${fileBasename} is the original file name "
                                 "(e.g. 'csforce/pistols/${fileBasename}')")
        parser.add_argument("--config", type=Path, metavar="TOML",
                            help="TOML file supplying defaults for any flag "
                                 "(explicit CLI values win)")
        parser.add_argument("--no-verify", action="store_true",
                            help="skip the post-merge verification gate")
        parser.add_argument("--dry-run", action="store_true",
                            help="discover, sanitise and load only; print the inventory")

    def run(self, args: argparse.Namespace) -> int:
        if args.config is not None:
            _apply_config(args)
        return run_merge_view(options_from(MergeViewOptions, args)).exit_code


_CONFIG_DEFAULTS: dict[str, object] = {
    "name": "v_merged",
    "exclude": [],
    "reference": None,
    "skip_unmatched": False,
    "shared_hands": False,
    "prune": False,
    "no_pool_bones": False,
    "manifest_format": "ini",
    "texture_budget": TEXTURE_BUDGET,
    "sequence_budget": None,
    "max_texture_size": None,
    "pack_textures": False,
    "no_pack_texture": [],
    "no_verify": False,
    "sound_path": None,
}


def _apply_config(args: argparse.Namespace) -> None:
    """Fill flags from a TOML config; explicit CLI values keep priority.

    A value is taken from the config only when the parsed argument still
    holds its default (so ``--flag`` on the command line always wins).
    """
    import tomllib

    with open(args.config, "rb") as handle:
        config = tomllib.load(handle)
    for key, value in config.items():
        attr = key.replace("-", "_")
        if attr not in _CONFIG_DEFAULTS:
            raise SystemExit(f"error: unknown config key {key!r}")
        if getattr(args, attr) == _CONFIG_DEFAULTS[attr]:
            if attr == "reference":
                value = Path(str(value))
            setattr(args, attr, value)


__all__ = ["MergeViewCommand"]
