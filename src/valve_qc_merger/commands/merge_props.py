"""CLI wiring for merge-props: effects, projectiles, props — and view models
whose hands are the model — merged into a few models, ``pev_body`` picking
the model. The work lives in :mod:`valve_qc_merger.services.merge_props`."""

from __future__ import annotations

import argparse
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.merge_view.parts import SHARED_HANDS_SEQUENCE_BUDGET, TEXTURE_BUDGET
from valve_qc_merger.services.base import options_from
from valve_qc_merger.services.merge_props import MergePropsOptions, run_merge_props


class MergePropsCommand(Command):
    """Merge decompiled effect / prop models into combined models."""

    name = "merge-props"
    help = ("merge a folder of decompiled effect, projectile and prop models (any rig) "
            "into combined models")

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("models_dir", type=Path,
                            help="parent directory; each subdir with one .qc is a model")
        parser.add_argument("--out", type=Path, required=True, help="output directory")
        parser.add_argument("--name", default="props", help="output model name stem")
        parser.add_argument("--exclude", action="append", default=[],
                            metavar="NAME", help="skip a model directory (repeatable)")
        parser.add_argument("--manifest-format", choices=("ini", "json", "toml"),
                            default="ini", help="per-model manifest format")
        parser.add_argument("--texture-budget", type=int, default=TEXTURE_BUDGET,
                            help=f"max textures per compiled part (default {TEXTURE_BUDGET})")
        parser.add_argument("--sequence-budget", type=int,
                            default=SHARED_HANDS_SEQUENCE_BUDGET,
                            help="max sequences per compiled part (default "
                                 f"{SHARED_HANDS_SEQUENCE_BUDGET})")
        parser.add_argument("--no-standalone", dest="standalone_rejects",
                            action="store_false",
                            help="leave out models too big to merge instead of shipping "
                                 "each on its own (standalone/<name>/)")
        parser.add_argument("--dry-run", action="store_true",
                            help="load and analyse only; print the inventory")

    def run(self, args: argparse.Namespace) -> int:
        return run_merge_props(options_from(MergePropsOptions, args)).exit_code


__all__ = ["MergePropsCommand"]
