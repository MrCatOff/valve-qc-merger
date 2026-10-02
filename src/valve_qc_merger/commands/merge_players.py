"""CLI wiring for merge-players.

Merges decompiled CSO player-character models into skin-bodygrouped CS 1.6
models: a donor rig (arctic) supplies the skeleton, the canonical animation set
(unneeded slots voided with a placeholder) and hitboxes; each source body is one
entry of a single ``skin`` bodygroup. Models are grouped by size / team / sex and
each group split into parts under the per-bodypart submodel limit. The work
lives in :mod:`valve_qc_merger.services.merge_players`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.merge_players.parts import DEFAULT_SUBMODEL_LIMIT, TEXTURE_BUDGET
from valve_qc_merger.merge_players.sequences import DEFAULT_PLACEHOLDER_GLOBS
from valve_qc_merger.services.base import options_from
from valve_qc_merger.services.merge_players import (
    MergePlayersOptions,
    run_merge_players,
)


class MergePlayersCommand(Command):
    """Merge decompiled CSO player-character models into skin-bodygrouped models."""

    name = "merge-players"
    help = "merge CSO player-character models into skin-bodygrouped CS 1.6 models"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("models_dir", type=Path,
                            help="parent directory; each subdir with one .qc is a model")
        parser.add_argument("--out", type=Path, required=True, help="output directory")
        parser.add_argument("--name", default="players", help="output model name stem")
        parser.add_argument("--base", type=Path, default=Path("storage/players_donor"),
                            help="donor rig directory (skeleton + canonical animations)")
        parser.add_argument("--group-by", choices=("size", "team", "sex"),
                            default="size", help="how to partition models into merges")
        parser.add_argument("--proportion-tolerance", type=float, default=2.0,
                            metavar="UNITS",
                            help="size mode: max per-bone length difference (units) "
                                 "for two rigs to share a group (default 2.0)")
        parser.add_argument("--labels", type=Path, metavar="TOML",
                            help="override team/sex labels: {model = \"ct\"|\"t\"|...}")
        parser.add_argument("--placeholder-seq", action="append", default=[],
                            metavar="GLOB",
                            help="void matching sequence slots with a placeholder "
                                 f"(repeatable; default {DEFAULT_PLACEHOLDER_GLOBS[0]!r})")
        parser.add_argument("--include-base", action="store_true",
                            help="include the donor body as skin 0")
        parser.add_argument("--max-skins", type=int, metavar="N",
                            help="cap skins per output part")
        parser.add_argument("--submodel-limit", type=int, default=DEFAULT_SUBMODEL_LIMIT,
                            help="per-bodypart submodel cap (default "
                                 f"{DEFAULT_SUBMODEL_LIMIT}; raise for a patched compiler)")
        parser.add_argument("--exclude", action="append", default=[],
                            metavar="NAME", help="skip a model directory (repeatable)")
        parser.add_argument("--manifest-format", choices=("ini", "json", "toml"),
                            default="ini", help="per-model manifest format")
        parser.add_argument("--texture-budget", type=int, default=TEXTURE_BUDGET,
                            help=f"max textures per part (default {TEXTURE_BUDGET})")
        parser.add_argument("--max-texture-size", type=int, metavar="N",
                            help="downscale staged textures larger than N (8-bit)")
        parser.add_argument("--pack-textures", action="store_true",
                            help="pack eligible textures into 512x512 atlases")
        parser.add_argument("--no-pack-texture", action="append", default=[],
                            metavar="GLOB", help="keep matching textures unpacked")
        parser.add_argument("--config", type=Path, metavar="TOML",
                            help="TOML file supplying defaults (explicit CLI wins)")
        parser.add_argument("--no-verify", action="store_true",
                            help="skip the post-merge verification gate")
        parser.add_argument("--dry-run", action="store_true",
                            help="discover and group only; print the plan")

    def run(self, args: argparse.Namespace) -> int:
        if args.config is not None:
            _apply_config(args)
        return run_merge_players(options_from(MergePlayersOptions, args)).exit_code

_CONFIG_DEFAULTS: dict[str, object] = {
    "name": "players", "group_by": "size", "proportion_tolerance": 2.0,
    "placeholder_seq": [], "include_base": False, "max_skins": None,
    "submodel_limit": DEFAULT_SUBMODEL_LIMIT, "exclude": [],
    "manifest_format": "ini", "texture_budget": TEXTURE_BUDGET,
    "max_texture_size": None, "pack_textures": False, "no_pack_texture": [],
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


__all__ = ["MergePlayersCommand"]
