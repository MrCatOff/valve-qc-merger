"""Exact structural skeleton operations for merge-view (spec §3.4–3.5).

Every operation here rewrites SMD data without moving anything: world
transforms of surviving bones are preserved per frame, exactly (verified by
the FK checks in the tests and by the pipeline's pose_preserved gate). The
math mirrors the battle-tested prior art (goldsource-models): removal folds
``removed_local · child_local`` into children per frame; reparenting re-solves
``local = parent_world⁻¹ · world``.

Operations take and return :class:`~valve_qc_merger.models.smd.Smd` values,
mutating in place for frames/nodes (the parser produces mutable models).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex
from valve_qc_merger.transform import Transform, matrix_to_euler

_IDENTITY = Transform.identity()


def _map_vertices(triangle: Triangle, fn: Callable[[Vertex], Vertex]) -> Triangle:
    a, b, c = triangle.vertices
    return dataclasses.replace(triangle, vertices=(fn(a), fn(b), fn(c)))


def fk_worlds(smd: Smd, frame: Frame) -> dict[int, Transform]:
    """World transform per bone index for one skeleton frame (any node order)."""
    local = {p.bone: Transform.from_pos_euler(p.position, p.rotation) for p in frame.poses}
    parent_of = {n.index: n.parent for n in smd.nodes}
    world: dict[int, Transform] = {}

    def resolve(index: int) -> Transform:
        cached = world.get(index)
        if cached is not None:
            return cached
        parent = parent_of[index]
        xf = local[index] if parent < 0 else resolve(parent).compose(local[index])
        world[index] = xf
        return xf

    for node in smd.nodes:
        resolve(node.index)
    return world


def rename_bones(smd: Smd, renames: dict[str, str]) -> None:
    """Apply a name mapping to the node table (frame data is index-based)."""
    smd.nodes = [
        Node(n.index, renames.get(n.name, n.name), n.parent) for n in smd.nodes
    ]


def remove_bones(smd: Smd, names: set[str]) -> None:
    """Delete bones, folding their transform into children per frame — exact.

    A removed bone's children are reparented to its parent with
    ``child_local' = removed_local · child_local`` in every frame, so every
    surviving bone's world transform is unchanged. Bones carrying triangle
    vertices must be rebound by the caller first (asserted here).
    """
    doomed = {n.index for n in smd.nodes if n.name in names}
    if not doomed:
        return
    used = {v.bone for t in smd.triangles for v in t.vertices}
    conflict = sorted(n.name for n in smd.nodes if n.index in doomed and n.index in used)
    if conflict:
        raise ValueError(f"cannot remove vertex-carrying bones: {conflict}")

    parent_of = {n.index: n.parent for n in smd.nodes}
    children_of: dict[int, list[int]] = {}
    for node in smd.nodes:
        children_of.setdefault(node.parent, []).append(node.index)

    # Process doomed bones parents-first so folds compose correctly when a
    # doomed bone's parent is also doomed.
    def depth(index: int) -> int:
        d, cursor = 0, parent_of[index]
        while cursor >= 0:
            d, cursor = d + 1, parent_of[cursor]
        return d

    # Work on mutable per-frame local dictionaries (Frame is frozen).
    frame_locals: list[dict[int, tuple[Vector3, Vector3]]] = [
        {p.bone: (p.position, p.rotation) for p in frame.poses}
        for frame in smd.frames
    ]
    for gone in sorted(doomed, key=depth):
        parent = parent_of[gone]
        for locals_ in frame_locals:
            gone_pos, gone_rot = locals_[gone]
            gone_local = Transform.from_pos_euler(gone_pos, gone_rot)
            for child in children_of.get(gone, []):
                child_pos, child_rot = locals_[child]
                folded = gone_local.compose(
                    Transform.from_pos_euler(child_pos, child_rot)
                )
                locals_[child] = (folded.translation, matrix_to_euler(folded.rotation))
        for child in children_of.get(gone, []):
            parent_of[child] = parent
            children_of.setdefault(parent, []).append(child)
        children_of[gone] = []

    keep = [n for n in smd.nodes if n.index not in doomed]
    smd.nodes = [Node(n.index, n.name, parent_of[n.index]) for n in keep]
    smd.frames = [
        Frame(frame.time, tuple(
            BonePose(i, *locals_[i])
            for i in sorted(locals_)
            if i not in doomed
        ))
        for frame, locals_ in zip(smd.frames, frame_locals, strict=True)
    ]
    renumber(smd)


def reparent_bone(smd: Smd, bone: str, new_parent: str | None) -> None:
    """Move ``bone`` under ``new_parent`` keeping its world pose, per frame."""
    index = next(n.index for n in smd.nodes if n.name == bone)
    parent_index = (
        -1 if new_parent is None
        else next(n.index for n in smd.nodes if n.name == new_parent)
    )
    # Cycle guard: the new parent must not be a descendant of the moved bone.
    parent_of = {n.index: n.parent for n in smd.nodes}
    cursor = parent_index
    while cursor >= 0:
        if cursor == index:
            raise ValueError(f"reparenting {bone!r} under its own descendant")
        cursor = parent_of[cursor]

    new_frames: list[Frame] = []
    for frame in smd.frames:
        worlds = fk_worlds(smd, frame)
        world = worlds[index]
        parent_world = worlds[parent_index] if parent_index >= 0 else _IDENTITY
        local = parent_world.inverse().compose(world)
        euler = matrix_to_euler(local.rotation)
        new_frames.append(Frame(frame.time, tuple(
            BonePose(p.bone, local.translation, euler) if p.bone == index else p
            for p in frame.poses
        )))
    smd.frames = new_frames
    smd.nodes = [
        Node(n.index, n.name, parent_index if n.index == index else n.parent)
        for n in smd.nodes
    ]
    renumber(smd)


def renumber(smd: Smd) -> None:
    """Rewrite ids contiguously, parents before children (studiomdl requires it)."""
    parent_of = {n.index: n.parent for n in smd.nodes}
    name_of = {n.index: n.name for n in smd.nodes}
    order: list[int] = []
    seen: set[int] = set()

    def visit(index: int) -> None:
        if index in seen:
            return
        parent = parent_of[index]
        if parent >= 0 and parent in parent_of:
            visit(parent)
        seen.add(index)
        order.append(index)

    for node in smd.nodes:
        visit(node.index)

    remap = {old: new for new, old in enumerate(order)}
    smd.nodes = [
        Node(remap[old], name_of[old],
             remap[parent_of[old]] if parent_of[old] in remap else -1)
        for old in order
    ]
    smd.frames = [
        Frame(f.time, tuple(sorted(
            (BonePose(remap[p.bone], p.position, p.rotation) for p in f.poses
             if p.bone in remap),
            key=lambda p: p.bone,
        )))
        for f in smd.frames
    ]
    smd.triangles = [
        _map_vertices(t, lambda v: dataclasses.replace(v, bone=remap[v.bone]))
        for t in smd.triangles
    ]


def ensure_root(smd: Smd, name: str = "Bip01") -> None:
    """Guarantee a single parentless root ``name``; reparent other roots under it.

    A missing root is inserted with an identity transform in every frame, so
    reparenting old roots under it is exact (their locals equal their worlds).
    A pre-existing bone of that name must already be a root.
    """
    existing = next((n for n in smd.nodes if n.name == name), None)
    if existing is not None and existing.parent != -1:
        raise ValueError(f"bone {name!r} exists but is not a root")
    if existing is None:
        index = max((n.index for n in smd.nodes), default=-1) + 1
        smd.nodes = [*smd.nodes, Node(index, name, -1)]
        zero = Vector3(0.0, 0.0, 0.0)
        smd.frames = [
            Frame(f.time, (*f.poses, BonePose(index, zero, zero)))
            for f in smd.frames
        ]
    for root_name in [n.name for n in smd.nodes if n.parent == -1 and n.name != name]:
        reparent_bone(smd, root_name, name)
    renumber(smd)


def rebind_vertices(smd: Smd, from_bone: str, to_bone: str) -> int:
    """Move every vertex bound to ``from_bone`` onto ``to_bone``; returns count."""
    src = next(n.index for n in smd.nodes if n.name == from_bone)
    dst = next(n.index for n in smd.nodes if n.name == to_bone)
    moved = sum(1 for t in smd.triangles for v in t.vertices if v.bone == src)
    smd.triangles = [
        _map_vertices(
            t, lambda v: dataclasses.replace(v, bone=dst if v.bone == src else v.bone)
        )
        for t in smd.triangles
    ]
    return moved


def world_positions(smd: Smd, frame: Frame) -> dict[str, Vector3]:
    """World head position per bone name for one frame (test/gate helper)."""
    worlds = fk_worlds(smd, frame)
    name_of = {n.index: n.name for n in smd.nodes}
    return {name_of[i]: t.translation for i, t in worlds.items()}


__all__ = [
    "ensure_root",
    "fk_worlds",
    "rename_bones",
    "remove_bones",
    "reparent_bone",
    "renumber",
    "rebind_vertices",
    "world_positions",
]
