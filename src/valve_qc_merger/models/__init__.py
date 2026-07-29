"""In-memory representations of GoldSource model data.

This package holds the data structures the tool works with:

* ``qc``     -- QC script directives (``$model``, ``$sequence``, ``$hbox``, ...)
* ``smd``    -- SMD geometry and animation (nodes, skeleton, triangles)
* ``hitbox`` -- hitbox groups and their bounding volumes
* ``geometry`` -- shared vector and bounding-box value types

Parsing raw files into these structures lives in
:mod:`valve_qc_merger.parsers`.
"""

from __future__ import annotations

from valve_qc_merger.models.geometry import BoundingBox, Vector2, Vector3
from valve_qc_merger.models.hitbox import Hitbox, HitGroup
from valve_qc_merger.models.qc import (
    Attachment,
    Body,
    BodyGroup,
    Directive,
    Origin,
    Qc,
    Sequence,
)
from valve_qc_merger.models.smd import (
    BonePose,
    Frame,
    Node,
    Smd,
    Triangle,
    Vertex,
)

__all__ = [
    "Attachment",
    "Body",
    "BodyGroup",
    "BonePose",
    "BoundingBox",
    "Directive",
    "Frame",
    "HitGroup",
    "Hitbox",
    "Node",
    "Origin",
    "Qc",
    "Sequence",
    "Smd",
    "Triangle",
    "Vector2",
    "Vector3",
    "Vertex",
]
