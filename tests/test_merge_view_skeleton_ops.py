"""merge-v skeleton operations: every edit must be FK-exact (spec §3.4-3.5)."""

from __future__ import annotations

import math

import pytest

from valve_qc_merger.merge_view.skeleton_ops import (
    rebind_vertices,
    remove_bones,
    rename_bones,
    renumber,
    reparent_bone,
    world_positions,
)
from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex


def _smd() -> Smd:
    # root -> arm -> hand -> {fingerA (with nub), fingerB}; two animated frames.
    nodes = [
        Node(0, "root", -1), Node(1, "arm", 0), Node(2, "hand", 1),
        Node(3, "fingerA", 2), Node(4, "nubA", 3), Node(5, "fingerB", 2),
    ]
    frames = []
    for t in range(3):
        a = 0.2 * t
        frames.append(Frame(t, (
            BonePose(0, Vector3(0, 0, 0), Vector3(0, 0, a)),
            BonePose(1, Vector3(2, 0, 0), Vector3(0, a / 2, 0)),
            BonePose(2, Vector3(1.5, 0.1, 0), Vector3(a, 0, 0)),
            BonePose(3, Vector3(0.8, 0.4, 0), Vector3(0, 0, a / 3)),
            BonePose(4, Vector3(0.3, 0, 0), Vector3(0, 0, 0)),
            BonePose(5, Vector3(0.8, -0.4, 0), Vector3(0, a / 4, 0)),
        )))
    tri = Triangle("skin.bmp", tuple(
        Vertex(3, Vector3(i, 0, 0), Vector3(0, 0, 1), Vector2(0, 0)) for i in range(3)
    ))
    return Smd(nodes=nodes, frames=frames, triangles=[tri])


def _close(a: Vector3, b: Vector3, tol: float = 1e-9) -> bool:
    return math.dist((a.x, a.y, a.z), (b.x, b.y, b.z)) < tol


def _assert_worlds_preserved(
    before: Smd, after: Smd, *, ignore: set[str] | None = None
) -> None:
    ignore = ignore or set()
    for f_before, f_after in zip(before.frames, after.frames, strict=True):
        b = world_positions(before, f_before)
        a = world_positions(after, f_after)
        for name, pos in b.items():
            if name in ignore:
                continue
            assert _close(pos, a[name]), (name, f_before.time)


def test_remove_leaf_and_mid_bone_preserves_world_poses() -> None:
    original = _smd()
    edited = _smd()
    rebind_vertices(edited, "fingerA", "hand")  # nubA is a leaf; fingerA carries verts
    remove_bones(edited, {"nubA", "fingerA"})
    assert {n.name for n in edited.nodes} == {"root", "arm", "hand", "fingerB"}
    _assert_worlds_preserved(original, edited, ignore={"nubA", "fingerA"})
    # ids contiguous, parents before children
    assert [n.index for n in edited.nodes] == list(range(4))
    for n in edited.nodes:
        assert n.parent < n.index


def test_remove_refuses_vertex_carrying_bone() -> None:
    smd = _smd()
    with pytest.raises(ValueError, match="fingerA"):
        remove_bones(smd, {"fingerA"})


def test_reparent_preserves_world_pose_and_guards_cycles() -> None:
    original = _smd()
    edited = _smd()
    reparent_bone(edited, "fingerB", "arm")  # skip over 'hand'
    assert next(n for n in edited.nodes if n.name == "fingerB").parent == \
        next(n.index for n in edited.nodes if n.name == "arm")
    _assert_worlds_preserved(original, edited)
    with pytest.raises(ValueError, match="descendant"):
        reparent_bone(edited, "arm", "fingerB")


def test_rename_and_renumber_keep_triangles_bound() -> None:
    smd = _smd()
    rename_bones(smd, {"fingerA": "Bip01 L Finger0"})
    assert any(n.name == "Bip01 L Finger0" for n in smd.nodes)
    renumber(smd)
    bound = {v.bone for t in smd.triangles for v in t.vertices}
    named = {n.index for n in smd.nodes if n.name == "Bip01 L Finger0"}
    assert bound == named
