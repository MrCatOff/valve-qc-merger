"""CLI wiring for merge-zhands.

Merges a folder of decompiled CSO zombie hand view models
(``v_<zombie>_knife[_variant]`` and ``v_<zombie>_grenade``) into ONE model:
a ``hands`` bodygroup with every distinct hand mesh and a shared ``grenade``
bodygroup, so ``pev_body = grenade_on + 2 * hands``. Inputs must already
be decompiled (one subdirectory with one .qc per model).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.merge_view.discovery import (
    MergeViewError,
    discover_models,
    load_model,
    sanitize_model_dir,
)
from valve_qc_merger.merge_zhands.merger import ZhandsError, merge_zhands

EXIT_OK = 0
EXIT_FAIL = 2
EXIT_DISCOVERY = 3


class MergeZhandsCommand(Command):
    """Merge decompiled zombie hand models into one bodygrouped model."""

    name = "merge-zhands"
    help = ("merge decompiled zombie hand view models (knife + grenade) into one "
            "model with a shared grenade")

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("models_dir", type=Path,
                            help="parent directory; each subdir with one .qc is a model")
        parser.add_argument("--out", type=Path, required=True, help="output directory")
        parser.add_argument("--name", default="v_zhands", help="output model name stem")
        parser.add_argument("--exclude", action="append", default=[],
                            metavar="NAME", help="skip a model directory (repeatable)")
        parser.add_argument("--grenade-prefix", default="frogbomb", metavar="TEXT",
                            help="texture-name prefix that marks grenade triangles "
                                 "(default: frogbomb)")
        parser.add_argument("--grenade-texture", default="frogbomb.bmp", metavar="BMP",
                            help="the shared grenade is taken from a model using "
                                 "this texture (default: frogbomb.bmp; falls back "
                                 "to the most common grenade)")
        parser.add_argument("--manifest-format", choices=("ini", "json", "toml"),
                            default="ini", help="per-model manifest format")

    def run(self, args: argparse.Namespace) -> int:
        try:
            model_dirs = discover_models(args.models_dir, exclude=set(args.exclude))
        except MergeViewError as exc:
            print(f"error: {exc}")
            return EXIT_DISCOVERY
        models = []
        for model_dir in model_dirs:
            sanitize_model_dir(model_dir)
            try:
                models.append(load_model(model_dir))
            except MergeViewError as exc:
                print(f"  {model_dir.name:<26} FAIL  {exc}")
                return EXIT_DISCOVERY
            print(f"  {model_dir.name:<26} OK    sequences={len(models[-1].anims)}")
        if not models:
            print("error: no models found")
            return EXIT_DISCOVERY
        try:
            report = merge_zhands(
                models, args.out, args.name,
                grenade_prefix=args.grenade_prefix,
                grenade_texture=args.grenade_texture,
                manifest_format=args.manifest_format,
            )
        except ZhandsError as exc:
            print(f"error: {exc}")
            return EXIT_FAIL
        for warning in report.warnings:
            print(f"  [Warning] {warning}")
        print(f"  {args.name}: bones={report.bones} hands={len(report.hands)} "
              f"grenade={report.grenade_from} sequences={report.sequences} "
              f"({report.sequences_deduped} shared) textures={report.textures}")
        print(f"  hands: {', '.join(report.hands)}")
        for check, passed, detail in report.gate:
            print(f"    verify {check:<20} {'PASS' if passed else 'FAIL'}  {detail}")
        return EXIT_OK if all(passed for _c, passed, _d in report.gate) else EXIT_FAIL
