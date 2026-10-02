"""CLI wiring for the ``canonicalize`` command.

Brings one decompiled view model's skeleton to the canonical rig while it
keeps its own hands (the per-model step of merge-v, written out on its own).
The work lives in :mod:`valve_qc_merger.services.canonicalize`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.services.base import options_from
from valve_qc_merger.services.canonicalize import CanonicalizeOptions, run_canonicalize


class CanonicalizeCommand(Command):
    """Rename/reparent a model's hand bones onto the canonical rig."""

    name = "canonicalize"
    help = ("bring a decompiled viewmodel's bones to the canonical rig "
            "(keeps its own hands; FK-exact, pose-checked)")

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--model-dir", type=Path, required=True,
                            help="decompiled model folder (QC + SMDs + textures)")
        parser.add_argument("--out", type=Path, required=True,
                            help="output folder (replaced): the model, canonicalised")
        parser.add_argument("--reference", type=Path,
                            help="canonical hand skeleton (default: merge-v's "
                                 "storage/hands/reference_hands.smd)")
        parser.add_argument("--prune", action="store_true",
                            help="also fold away vertex-less unreferenced bones")

    def run(self, args: argparse.Namespace) -> int:
        return run_canonicalize(options_from(CanonicalizeOptions, args)).exit_code


__all__ = ["CanonicalizeCommand"]
