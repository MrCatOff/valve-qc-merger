"""Data structures for QC files (the StudioMdl compile script).

A QC file is a list of directives that tell ``studiomdl`` how to assemble a
``.mdl`` from SMD sources: which geometry to include, how to place and scale it,
which animations (sequences) to compile, where the hitboxes and attachments go,
and so on.

:class:`Qc` models the directives this tool understands as typed fields, and
keeps everything else verbatim in :attr:`Qc.unknown` so nothing is silently
lost when reading a file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from valve_qc_merger.models.geometry import BoundingBox, Vector3
from valve_qc_merger.models.hitbox import Hitbox


@dataclass(frozen=True, slots=True)
class Body:
    """A body model: geometry from a single SMD (``$body`` / ``studio``)."""

    name: str
    source: str


@dataclass(frozen=True, slots=True)
class BodyGroup:
    """A ``$bodygroup``: switchable bodies plus an optional ``blank`` slot."""

    name: str
    bodies: tuple[Body, ...]
    has_blank: bool = False


@dataclass(frozen=True, slots=True)
class Sequence:
    """A ``$sequence``: an animation compiled into the model."""

    name: str
    sources: tuple[str, ...]
    fps: float | None = None
    loop: bool = False
    activity: str | None = None
    activity_weight: int | None = None
    options: tuple[str, ...] = ()

    @property
    def source(self) -> str | None:
        """The first (often only) SMD source, or ``None`` if there is none."""
        return self.sources[0] if self.sources else None


@dataclass(frozen=True, slots=True)
class Attachment:
    """An ``$attachment``: a named point offset from a bone."""

    index: int
    bone: str
    offset: Vector3


@dataclass(frozen=True, slots=True)
class Origin:
    """The ``$origin`` directive: a translation with an optional rotation."""

    position: Vector3
    rotation: float | None = None


@dataclass(frozen=True, slots=True)
class Directive:
    """A raw QC directive kept verbatim: its name and its argument tokens."""

    name: str
    args: tuple[str, ...]


@dataclass(slots=True)
class Qc:
    """A parsed QC file."""

    modelname: str | None = None
    cd: str | None = None
    cd_texture: list[str] = field(default_factory=list)
    scale: float | None = None
    origin: Origin | None = None
    eye_position: Vector3 | None = None
    bbox: BoundingBox | None = None
    cbox: BoundingBox | None = None
    flags: int | None = None
    bodies: list[Body] = field(default_factory=list)
    body_groups: list[BodyGroup] = field(default_factory=list)
    sequences: list[Sequence] = field(default_factory=list)
    hitboxes: list[Hitbox] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    unknown: list[Directive] = field(default_factory=list)

    @property
    def sequence_names(self) -> list[str]:
        """Names of every sequence, in declaration order."""
        return [sequence.name for sequence in self.sequences]

    def find_sequence(self, name: str) -> Sequence | None:
        """Return the sequence named ``name``, or ``None``."""
        for sequence in self.sequences:
            if sequence.name == name:
                return sequence
        return None

    def hitboxes_by_group(self) -> dict[int, list[Hitbox]]:
        """Group hitboxes by their hit group, preserving order within a group."""
        grouped: dict[int, list[Hitbox]] = {}
        for hitbox in self.hitboxes:
            grouped.setdefault(hitbox.group, []).append(hitbox)
        return grouped

    def sources(self) -> list[str]:
        """Every SMD referenced by bodies, body groups and sequences (unique)."""
        seen: dict[str, None] = {}
        for body in self.bodies:
            seen.setdefault(body.source, None)
        for group in self.body_groups:
            for body in group.bodies:
                seen.setdefault(body.source, None)
        for sequence in self.sequences:
            for source in sequence.sources:
                seen.setdefault(source, None)
        return list(seen)


__all__ = [
    "Attachment",
    "Body",
    "BodyGroup",
    "Directive",
    "Origin",
    "Qc",
    "Sequence",
]
