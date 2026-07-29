"""Data structures for SMD files (StudioMdl Data).

An SMD file describes either reference geometry or an animation. Both share the
same three-block layout::

    nodes      -- the skeleton hierarchy (bone id, name, parent id)
    skeleton   -- one or more time frames of per-bone position/rotation
    triangles  -- textured geometry (present only in reference SMDs)

A reference SMD carries geometry and a single skeleton frame (the bind pose);
an animation SMD carries many skeleton frames and no geometry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from valve_qc_merger.models.geometry import BoundingBox, Vector2, Vector3

# Bone id used by SMD to mean "no parent" (a root bone).
ROOT_PARENT: int = -1


@dataclass(frozen=True, slots=True)
class Node:
    """A single bone in the skeleton hierarchy."""

    index: int
    name: str
    parent: int

    @property
    def is_root(self) -> bool:
        """Whether this bone has no parent."""
        return self.parent == ROOT_PARENT


@dataclass(frozen=True, slots=True)
class BonePose:
    """Position and Euler rotation of one bone within a single frame."""

    bone: int
    position: Vector3
    rotation: Vector3


@dataclass(frozen=True, slots=True)
class Frame:
    """One ``time`` block of the skeleton: a pose for every bone."""

    time: int
    poses: tuple[BonePose, ...]

    def pose_for(self, bone: int) -> BonePose | None:
        """Return the pose for ``bone`` in this frame, or ``None``."""
        for pose in self.poses:
            if pose.bone == bone:
                return pose
        return None


@dataclass(frozen=True, slots=True)
class Vertex:
    """A single triangle vertex bound to one bone."""

    bone: int
    position: Vector3
    normal: Vector3
    uv: Vector2


@dataclass(frozen=True, slots=True)
class Triangle:
    """A textured triangle: a material name and exactly three vertices."""

    material: str
    vertices: tuple[Vertex, Vertex, Vertex]


@dataclass(slots=True)
class Smd:
    """A parsed SMD file."""

    version: int = 1
    nodes: list[Node] = field(default_factory=list)
    frames: list[Frame] = field(default_factory=list)
    triangles: list[Triangle] = field(default_factory=list)

    @property
    def bone_count(self) -> int:
        """Number of bones declared in the ``nodes`` block."""
        return len(self.nodes)

    @property
    def frame_count(self) -> int:
        """Number of animation frames in the ``skeleton`` block."""
        return len(self.frames)

    @property
    def has_geometry(self) -> bool:
        """Whether the file contains any triangles."""
        return bool(self.triangles)

    @property
    def is_reference(self) -> bool:
        """A reference SMD has geometry (and, conventionally, one frame)."""
        return self.has_geometry

    @property
    def is_animation(self) -> bool:
        """An animation SMD has frames but no geometry."""
        return not self.has_geometry and self.frame_count > 0

    @property
    def root_nodes(self) -> list[Node]:
        """Bones that have no parent."""
        return [node for node in self.nodes if node.is_root]

    def node_by_index(self, index: int) -> Node | None:
        """Return the bone with ``index``, or ``None`` if there is none."""
        for node in self.nodes:
            if node.index == index:
                return node
        return None

    def node_by_name(self, name: str) -> Node | None:
        """Return the bone named ``name``, or ``None`` if there is none."""
        for node in self.nodes:
            if node.name == name:
                return node
        return None

    def materials(self) -> list[str]:
        """Unique material names used by the geometry, in first-seen order."""
        seen: dict[str, None] = {}
        for triangle in self.triangles:
            seen.setdefault(triangle.material, None)
        return list(seen)

    def bounding_box(self) -> BoundingBox | None:
        """Axis-aligned bounds of all vertex positions, or ``None`` if empty."""
        vertices = [vertex for triangle in self.triangles for vertex in triangle.vertices]
        if not vertices:
            return None
        mins = maxs = vertices[0].position
        for vertex in vertices[1:]:
            mins = Vector3.component_min(mins, vertex.position)
            maxs = Vector3.component_max(maxs, vertex.position)
        return BoundingBox(mins, maxs)


__all__ = [
    "ROOT_PARENT",
    "BonePose",
    "Frame",
    "Node",
    "Smd",
    "Triangle",
    "Vertex",
]
