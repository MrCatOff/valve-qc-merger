"""Subcommand registry.

Register new commands by appending their class to :data:`_COMMAND_CLASSES`.
The CLI discovers them through :func:`iter_commands`.
"""

from __future__ import annotations

from collections.abc import Iterator

from valve_qc_merger.commands.base import Command
from valve_qc_merger.commands.clear_weapon import ClearWeaponCommand
from valve_qc_merger.commands.move_weapon import MoveWeaponCommand
from valve_qc_merger.commands.replace_hands import ReplaceHandsCommand

# Command classes exposed by the CLI, in display order.
_COMMAND_CLASSES: list[type[Command]] = [
    ReplaceHandsCommand,
    MoveWeaponCommand,
    ClearWeaponCommand,
]


def iter_commands() -> Iterator[Command]:
    """Yield one instance of every registered command."""
    for command_class in _COMMAND_CLASSES:
        yield command_class()


__all__ = ["Command", "iter_commands"]
