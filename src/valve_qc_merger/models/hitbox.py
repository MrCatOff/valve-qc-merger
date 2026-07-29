"""Hitbox data as declared by the QC ``$hbox`` directive.

A hitbox attaches an axis-aligned box to a bone and assigns it to a hit group.
GoldSource games (Counter-Strike 1.6 included) use the hit group to scale
damage -- notably headshots via :attr:`HitGroup.HEAD`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from valve_qc_merger.models.geometry import BoundingBox, Vector3


class HitGroup(IntEnum):
    """Standard GoldSource hit groups used for damage scaling."""

    GENERIC = 0
    HEAD = 1
    CHEST = 2
    STOMACH = 3
    LEFT_ARM = 4
    RIGHT_ARM = 5
    LEFT_LEG = 6
    RIGHT_LEG = 7

    @classmethod
    def from_value(cls, value: int) -> HitGroup | int:
        """Return the matching :class:`HitGroup`, or the raw int if unknown."""
        try:
            return cls(value)
        except ValueError:
            return value


@dataclass(frozen=True, slots=True)
class Hitbox:
    """A single ``$hbox`` entry: a box on a bone within a hit group."""

    group: int
    bone: str
    mins: Vector3
    maxs: Vector3

    @property
    def hit_group(self) -> HitGroup | int:
        """The hit group as a :class:`HitGroup` when recognised."""
        return HitGroup.from_value(self.group)

    @property
    def bounds(self) -> BoundingBox:
        """The hitbox volume as a :class:`BoundingBox`."""
        return BoundingBox(self.mins, self.maxs)

    @property
    def is_head(self) -> bool:
        """Whether this hitbox belongs to the head hit group."""
        return self.group == HitGroup.HEAD


__all__ = ["HitGroup", "Hitbox"]
