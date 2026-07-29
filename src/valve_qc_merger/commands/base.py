"""Base types shared by CLI subcommands."""

from __future__ import annotations

import argparse
from abc import ABC, abstractmethod


class Command(ABC):
    """A single CLI subcommand.

    Subclasses declare a ``name`` and ``help`` string, wire up their own
    arguments in :meth:`configure`, and do the work in :meth:`run`.
    """

    name: str
    help: str

    def register(
        self, subparsers: argparse._SubParsersAction[argparse.ArgumentParser]
    ) -> None:
        """Attach this command to the top-level subparser collection."""
        parser = subparsers.add_parser(self.name, help=self.help, description=self.help)
        self.configure(parser)
        parser.set_defaults(handler=self.run)

    def configure(self, parser: argparse.ArgumentParser) -> None:  # noqa: B027
        """Register command-specific arguments. Optional override hook."""

    @abstractmethod
    def run(self, args: argparse.Namespace) -> int:
        """Execute the command and return a process exit code."""
