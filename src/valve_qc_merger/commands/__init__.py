"""Subcommand registry.

Register new commands by appending their class to :data:`_COMMAND_CLASSES`.
The CLI discovers them through :func:`iter_commands`.
"""

from __future__ import annotations

from collections.abc import Iterator

from valve_qc_merger.commands.base import Command
from valve_qc_merger.commands.merge_player import MergePlayerCommand
from valve_qc_merger.commands.merge_players import MergePlayersCommand
from valve_qc_merger.commands.merge_view import MergeViewCommand
from valve_qc_merger.commands.merge_world import MergeWorldCommand
from valve_qc_merger.commands.retarget import RetargetCommand

# Command classes exposed by the CLI, in display order.
_COMMAND_CLASSES: list[type[Command]] = [
    RetargetCommand,
    MergeViewCommand,
    MergePlayerCommand,
    MergePlayersCommand,
    MergeWorldCommand,
]


def iter_commands() -> Iterator[Command]:
    """Yield one instance of every registered command."""
    for command_class in _COMMAND_CLASSES:
        yield command_class()


__all__ = ["Command", "iter_commands"]
