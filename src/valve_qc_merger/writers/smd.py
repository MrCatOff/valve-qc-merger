"""Serializer for :class:`valve_qc_merger.models.smd.Smd` back to SMD text.

Output uses fixed six-decimal formatting, matching the style StudioMdl tools
(e.g. Crowbar) emit, so a parse/write round-trip is numerically faithful.
"""

from __future__ import annotations

from pathlib import Path

from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import Smd


def _fmt(value: float) -> str:
    return f"{value:.6f}"


def _fmt_vec3(vector: Vector3) -> str:
    return f"{_fmt(vector.x)} {_fmt(vector.y)} {_fmt(vector.z)}"


def _fmt_vec2(vector: Vector2) -> str:
    return f"{_fmt(vector.u)} {_fmt(vector.v)}"


def write_smd_text(smd: Smd) -> str:
    """Serialize an :class:`Smd` to SMD text (including a trailing newline)."""
    lines: list[str] = [f"version {smd.version}"]

    lines.append("nodes")
    for node in smd.nodes:
        lines.append(f'{node.index} "{node.name}" {node.parent}')
    lines.append("end")

    lines.append("skeleton")
    for frame in smd.frames:
        lines.append(f"time {frame.time}")
        for pose in frame.poses:
            lines.append(
                f"{pose.bone} {_fmt_vec3(pose.position)} {_fmt_vec3(pose.rotation)}"
            )
    lines.append("end")

    if smd.triangles:
        lines.append("triangles")
        for triangle in smd.triangles:
            lines.append(triangle.material)
            for vertex in triangle.vertices:
                lines.append(
                    f"{vertex.bone} {_fmt_vec3(vertex.position)} "
                    f"{_fmt_vec3(vertex.normal)} {_fmt_vec2(vertex.uv)}"
                )
        lines.append("end")

    return "\n".join(lines) + "\n"


def write_smd_file(smd: Smd, path: str | Path) -> None:
    """Write an :class:`Smd` to disk."""
    Path(path).write_text(write_smd_text(smd), encoding="latin-1")


__all__ = ["write_smd_file", "write_smd_text"]
