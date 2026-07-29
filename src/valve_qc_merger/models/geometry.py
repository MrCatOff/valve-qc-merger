"""Small immutable geometry value types shared across the model classes.

GoldSource stores coordinates as three floats and texture coordinates as two.
These types wrap those tuples so the rest of the code can pass around named,
typed values instead of bare ``(x, y, z)`` tuples.
"""

from __future__ import annotations

import math
from typing import NamedTuple


class Vector2(NamedTuple):
    """A 2D texture coordinate (UV)."""

    u: float
    v: float


class Vector3(NamedTuple):
    """A 3D point or direction in GoldSource world units."""

    x: float
    y: float
    z: float

    def length(self) -> float:
        """Euclidean length of the vector."""
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)

    def distance_to(self, other: Vector3) -> float:
        """Distance between this point and ``other``."""
        return math.sqrt(
            (self.x - other.x) ** 2
            + (self.y - other.y) ** 2
            + (self.z - other.z) ** 2
        )

    def midpoint(self, other: Vector3) -> Vector3:
        """Point halfway between this vector and ``other``."""
        return Vector3(
            (self.x + other.x) / 2.0,
            (self.y + other.y) / 2.0,
            (self.z + other.z) / 2.0,
        )

    @staticmethod
    def component_min(a: Vector3, b: Vector3) -> Vector3:
        """Per-component minimum of two vectors."""
        return Vector3(min(a.x, b.x), min(a.y, b.y), min(a.z, b.z))

    @staticmethod
    def component_max(a: Vector3, b: Vector3) -> Vector3:
        """Per-component maximum of two vectors."""
        return Vector3(max(a.x, b.x), max(a.y, b.y), max(a.z, b.z))


class BoundingBox(NamedTuple):
    """An axis-aligned box described by its minimum and maximum corners."""

    mins: Vector3
    maxs: Vector3

    @property
    def size(self) -> Vector3:
        """Extent of the box along each axis."""
        return Vector3(
            self.maxs.x - self.mins.x,
            self.maxs.y - self.mins.y,
            self.maxs.z - self.mins.z,
        )

    @property
    def center(self) -> Vector3:
        """Geometric center of the box."""
        return self.mins.midpoint(self.maxs)


__all__ = ["BoundingBox", "Vector2", "Vector3"]
