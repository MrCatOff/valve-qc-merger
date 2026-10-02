"""CLI wiring for ``decompile``: GoldSource .mdl -> QC + SMD + BMP, in process.

A pure-Python replacement for an external decompiler (so it also works inside
the frozen Windows exe). The work lives in
:mod:`valve_qc_merger.services.decompile`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.services.base import options_from
from valve_qc_merger.services.decompile import DecompileOptions, run_decompile


class DecompileCommand(Command):
    """Decompile .mdl files into QC + SMD + BMP folders."""

    name = "decompile"
    help = "decompile GoldSource .mdl files (one file or a folder) into QC + SMD + BMP"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("source", type=Path,
                            help="a .mdl file, or a folder searched recursively "
                                 "(T.mdl / NN.mdl companions are read with their model)")
        parser.add_argument("--out", type=Path, required=True,
                            help="output directory; each model lands in OUT/<model>/")

    def run(self, args: argparse.Namespace) -> int:
        return run_decompile(options_from(DecompileOptions, args)).exit_code


__all__ = ["DecompileCommand"]
