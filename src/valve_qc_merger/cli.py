"""Command-line interface for valve-qc-merger.

The CLI is built on :mod:`argparse` with a small subcommand registry so new
commands (parsing QC, inspecting SMDs, editing hitboxes, ...) can be added
without touching the top-level parser wiring.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from valve_qc_merger import __version__
from valve_qc_merger.commands import iter_commands

PROG = "valve-qc-merger"


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser with every registered command."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Tools for GoldSource QC files, SMDs and hitboxes "
            "(Counter-Strike 1.6 models)."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for command in iter_commands():
        command.register(subparsers)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Program entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return 2

    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
