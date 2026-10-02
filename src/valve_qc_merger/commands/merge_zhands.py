"""CLI wiring for merge-zhands.

Merges a folder of decompiled CSO zombie hand view models
(``v_<zombie>_knife[_variant]`` and ``v_<zombie>_grenade``) into ONE model:
a ``hands`` bodygroup with every distinct hand mesh and a shared ``grenade``
bodygroup, so ``pev_body = grenade_on + 2 * hands``. Inputs must already
be decompiled (one subdirectory with one .qc per model). The work lives in
:mod:`valve_qc_merger.services.merge_zhands`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.services.base import options_from
from valve_qc_merger.services.merge_zhands import MergeZhandsOptions, run_merge_zhands


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
        return run_merge_zhands(options_from(MergeZhandsOptions, args)).exit_code

__all__ = ["MergeZhandsCommand"]
