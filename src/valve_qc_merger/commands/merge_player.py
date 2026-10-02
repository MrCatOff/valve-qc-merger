"""CLI wiring for merge-p.

Merges a folder of decompiled p_ (player-held) weapon models into as few
compilable .mdl files as studiomdl's hard limits allow. The engine bone-merges
the shared ``Bip01`` chain by name against the player model at runtime, so a
part carries that chain once, ONE uniquely-named bone per held object (two for
dual-wield), one geometry submodel per weapon behind a single ``weapons``
bodygroup (``pev_body 0`` = blank), and a single-frame ``idle`` sequence. The
work lives in :mod:`valve_qc_merger.services.merge_player`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.merge_player.loading import load_player_model
from valve_qc_merger.merge_player.parts import TEXTURE_BUDGET
from valve_qc_merger.services.base import options_from
from valve_qc_merger.services.merge_player import MergePlayerOptions, run_merge_player

# Kept for callers that predate the service layer.
_load_player_model = load_player_model


class MergePlayerCommand(Command):
    """Merge decompiled p_ weapon models into combined bodygrouped models."""

    name = "merge-p"
    help = "merge a folder of decompiled p_ (player) weapon models into combined models"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("models_dir", type=Path,
                            help="parent directory; each subdir with one .qc is a model")
        parser.add_argument("--out", type=Path, required=True, help="output directory")
        parser.add_argument("--name", default="p_merged", help="output model name stem")
        parser.add_argument("--exclude", action="append", default=[],
                            metavar="NAME", help="skip a model directory (repeatable)")
        parser.add_argument("--manifest-format", choices=("ini", "json", "toml"),
                            default="ini", help="per-model manifest format")
        parser.add_argument("--texture-budget", type=int, default=TEXTURE_BUDGET,
                            help="max textures per compiled part (default "
                                 f"{TEXTURE_BUDGET}; hard engine cap is 100)")
        parser.add_argument("--max-texture-size", type=int, metavar="N",
                            help="downscale staged textures larger than N on "
                                 "either axis (8-bit re-quantised)")
        parser.add_argument("--pack-textures", action="store_true",
                            help="pack eligible textures four-to-a-file into "
                                 "512x512 atlases (skin-family textures stay "
                                 "standalone)")
        parser.add_argument("--no-pack-texture", action="append", default=[],
                            metavar="GLOB",
                            help="keep matching textures out of atlases "
                                 "(repeatable)")
        parser.add_argument("--config", type=Path, metavar="TOML",
                            help="TOML file supplying defaults for any flag "
                                 "(explicit CLI values win)")
        parser.add_argument("--no-verify", action="store_true",
                            help="skip the post-merge verification gate")
        parser.add_argument("--dry-run", action="store_true",
                            help="discover, sanitise and analyse only; print "
                                 "the inventory")

    def run(self, args: argparse.Namespace) -> int:
        if args.config is not None:
            _apply_config(args)
        return run_merge_player(options_from(MergePlayerOptions, args)).exit_code

_CONFIG_DEFAULTS: dict[str, object] = {
    "name": "p_merged",
    "exclude": [],
    "manifest_format": "ini",
    "texture_budget": TEXTURE_BUDGET,
    "max_texture_size": None,
    "pack_textures": False,
    "no_pack_texture": [],
    "no_verify": False,
}


def _apply_config(args: argparse.Namespace) -> None:
    """Fill flags from a TOML config; explicit CLI values keep priority."""
    import tomllib

    with open(args.config, "rb") as handle:
        config = tomllib.load(handle)
    for key, value in config.items():
        attr = key.replace("-", "_")
        if attr not in _CONFIG_DEFAULTS:
            raise SystemExit(f"error: unknown config key {key!r}")
        if getattr(args, attr) == _CONFIG_DEFAULTS[attr]:
            setattr(args, attr, value)


__all__ = ["MergePlayerCommand"]
