"""Parser for SMD files into :class:`valve_qc_merger.models.smd.Smd`.

The grammar is the standard three-block StudioMdl layout::

    version 1
    nodes
        <id> "<name>" <parent>
        ...
    end
    skeleton
        time <n>
            <bone> <px> <py> <pz> <rx> <ry> <rz>
            ...
    end
    triangles
        <material>
        <bone> <px> <py> <pz> <nx> <ny> <nz> <u> <v>
        <bone> ...
        <bone> ...
        ...
    end

Reference SMDs carry ``triangles`` and one ``skeleton`` frame; animation SMDs
carry many ``skeleton`` frames and no ``triangles``.
"""

from __future__ import annotations

from pathlib import Path

from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex


class SmdParseError(ValueError):
    """Raised when an SMD file does not match the expected grammar."""


def _strip_comment(line: str) -> str:
    """Remove a trailing ``//`` comment, keeping ``#`` (used in file names)."""
    index = line.find("//")
    return line if index == -1 else line[:index]


def parse_smd_text(text: str) -> Smd:
    """Parse the contents of an SMD file."""
    lines = text.splitlines()
    smd = Smd()
    index = 0
    total = len(lines)

    while index < total:
        raw = _strip_comment(lines[index]).strip()
        index += 1
        if not raw:
            continue

        keyword = raw.split(None, 1)[0]
        if keyword == "version":
            smd.version = int(raw.split()[1])
        elif keyword == "nodes":
            smd.nodes, index = _parse_nodes(lines, index)
        elif keyword == "skeleton":
            smd.frames, index = _parse_skeleton(lines, index)
        elif keyword == "triangles":
            smd.triangles, index = _parse_triangles(lines, index)
        # Unknown top-level keywords are ignored so uncommon blocks do not abort a parse.

    return smd


def parse_smd_file(path: str | Path) -> Smd:
    """Read and parse an SMD file from disk."""
    return parse_smd_text(Path(path).read_text(encoding="latin-1"))


def _parse_nodes(lines: list[str], index: int) -> tuple[list[Node], int]:
    nodes: list[Node] = []
    total = len(lines)
    while index < total:
        raw = _strip_comment(lines[index]).strip()
        index += 1
        if not raw:
            continue
        if raw == "end":
            break
        node_index, name, parent = _parse_node_line(raw)
        nodes.append(Node(node_index, name, parent))
    return nodes, index


def _parse_node_line(raw: str) -> tuple[int, str, int]:
    quote_start = raw.find('"')
    quote_end = raw.find('"', quote_start + 1)
    if quote_start == -1 or quote_end == -1:
        raise SmdParseError(f"malformed nodes line: {raw!r}")
    name = raw[quote_start + 1 : quote_end]
    try:
        node_index = int(raw[:quote_start].split()[0])
        parent = int(raw[quote_end + 1 :].split()[0])
    except (IndexError, ValueError) as exc:
        raise SmdParseError(f"malformed nodes line: {raw!r}") from exc
    return node_index, name, parent


def _parse_skeleton(lines: list[str], index: int) -> tuple[list[Frame], int]:
    frames: list[Frame] = []
    total = len(lines)
    current_time: int | None = None
    poses: list[BonePose] = []

    while index < total:
        raw = _strip_comment(lines[index]).strip()
        index += 1
        if not raw:
            continue
        if raw == "end":
            break
        if raw.split(None, 1)[0] == "time":
            if current_time is not None:
                frames.append(Frame(current_time, tuple(poses)))
            current_time = int(raw.split()[1])
            poses = []
            continue
        poses.append(_parse_pose_line(raw))

    if current_time is not None:
        frames.append(Frame(current_time, tuple(poses)))
    return frames, index


def _parse_pose_line(raw: str) -> BonePose:
    parts = raw.split()
    if len(parts) < 7:
        raise SmdParseError(f"malformed skeleton line: {raw!r}")
    try:
        bone = int(parts[0])
        values = [float(part) for part in parts[1:7]]
    except ValueError as exc:
        raise SmdParseError(f"malformed skeleton line: {raw!r}") from exc
    return BonePose(
        bone=bone,
        position=Vector3(values[0], values[1], values[2]),
        rotation=Vector3(values[3], values[4], values[5]),
    )


def _parse_triangles(lines: list[str], index: int) -> tuple[list[Triangle], int]:
    triangles: list[Triangle] = []
    total = len(lines)

    while index < total:
        material = _strip_comment(lines[index]).strip()
        index += 1
        if not material:
            continue
        if material == "end":
            break
        vertices, index = _parse_triangle_vertices(lines, index, material)
        triangles.append(Triangle(material=material, vertices=vertices))

    return triangles, index


def _parse_triangle_vertices(
    lines: list[str], index: int, material: str
) -> tuple[tuple[Vertex, Vertex, Vertex], int]:
    collected: list[Vertex] = []
    total = len(lines)
    while len(collected) < 3 and index < total:
        raw = _strip_comment(lines[index]).strip()
        index += 1
        if not raw:
            continue
        collected.append(_parse_vertex_line(raw))
    if len(collected) != 3:
        raise SmdParseError(f"incomplete triangle for material {material!r}")
    return (collected[0], collected[1], collected[2]), index


def _parse_vertex_line(raw: str) -> Vertex:
    parts = raw.split()
    if len(parts) < 9:
        raise SmdParseError(f"malformed triangle vertex line: {raw!r}")
    try:
        bone = int(parts[0])
        values = [float(part) for part in parts[1:9]]
    except ValueError as exc:
        raise SmdParseError(f"malformed triangle vertex line: {raw!r}") from exc
    return Vertex(
        bone=bone,
        position=Vector3(values[0], values[1], values[2]),
        normal=Vector3(values[3], values[4], values[5]),
        uv=Vector2(values[6], values[7]),
    )


__all__ = ["SmdParseError", "parse_smd_file", "parse_smd_text"]
