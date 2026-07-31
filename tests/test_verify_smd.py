"""Phase 6 post-export verification tests (§7.8), no Blender."""

from __future__ import annotations

import math
from pathlib import Path

from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex
from valve_qc_merger.retarget.verify_smd import verify_export
from valve_qc_merger.writers.smd import write_smd_file

# Reference skeleton: root -> hand. The mesh/anim skeleton appends a gun bone.
_REF_NODES = [Node(0, "root", -1), Node(1, "hand", 0)]
_UNI_NODES = [Node(0, "root", -1), Node(1, "hand", 0), Node(2, "gun", 1)]
_REST = {0: Vector3(0, 0, 0), 1: Vector3(0, 1, 0), 2: Vector3(0.5, 0, 0)}


def _tri(bone: int, base: float) -> Triangle:
    v = tuple(
        Vertex(bone, Vector3(base + i, 1.0, 1.0), Vector3(0, 0, 1), Vector2(0.1 * i, 0.2))
        for i in range(3)
    )
    return Triangle("hand.bmp", v)  # type: ignore[arg-type]


_REF_TRIS = [_tri(1, 1.0), _tri(1, 5.0)]


def _rest_pose(nodes: list[Node]) -> Frame:
    return Frame(0, tuple(BonePose(n.index, _REST[n.index], Vector3(0, 0, 0)) for n in nodes))


def _reference() -> Smd:
    return Smd(nodes=_REF_NODES, frames=[_rest_pose(_REF_NODES)], triangles=list(_REF_TRIS))


def _mesh() -> Smd:
    weapon = Triangle("WPN.bmp", tuple(  # type: ignore[arg-type]
        Vertex(2, Vector3(9 + i, 9, 9), Vector3(0, 0, 1), Vector2(0, 0)) for i in range(3)
    ))
    return Smd(nodes=_UNI_NODES, frames=[_rest_pose(_UNI_NODES)],
               triangles=[*_REF_TRIS, weapon])


def _anim(*, hand_drift: float = 0.0, rot_jump: bool = False) -> Smd:
    frames = []
    for t in range(4):
        poses = []
        for n in _UNI_NODES:
            pos = _REST[n.index]
            rot = Vector3(0, 0, 0)
            if n.index == 1 and hand_drift and t == 2:
                pos = Vector3(pos.x + hand_drift, pos.y, pos.z)
            if n.index == 0:  # anchor may move/rotate freely
                rot = Vector3(0, 0, 0.1 * t)
            if n.index == 2 and rot_jump and t == 2:
                rot = Vector3(0, 0, math.radians(179))
            poses.append(BonePose(n.index, pos, rot))
        frames.append(Frame(t, tuple(poses)))
    return Smd(nodes=_UNI_NODES, frames=frames)


def _run(tmp_path: Path, mesh: Smd, anim: Smd) -> object:
    mesh_p = tmp_path / "model-PV.smd"
    anim_p = tmp_path / "idle.smd"
    ref_p = tmp_path / "reference.smd"
    write_smd_file(mesh, mesh_p)
    write_smd_file(anim, anim_p)
    write_smd_file(_reference(), ref_p)
    return verify_export(
        mesh_p, {"idle": anim_p}, ref_p,
        hand_bones={"root", "hand"}, anchor_bones={"root"},
    )


def test_clean_export_passes_every_check(tmp_path: Path) -> None:
    res = _run(tmp_path, _mesh(), _anim())
    assert res.ok  # type: ignore[attr-defined]
    assert all(res.checks.values())  # type: ignore[attr-defined]


def test_node_table_mismatch_fails(tmp_path: Path) -> None:
    anim = _anim()
    anim.nodes = [Node(0, "root", -1), Node(1, "hand", 0), Node(2, "gun", 0)]  # gun re-parented
    res = _run(tmp_path, _mesh(), anim)
    assert not res.ok  # type: ignore[attr-defined]
    assert res.checks["node_tables_identical"] is False  # type: ignore[attr-defined]


def test_hand_translation_drift_fails(tmp_path: Path) -> None:
    res = _run(tmp_path, _mesh(), _anim(hand_drift=0.5))
    assert res.checks["hand_translation_frozen"] is False  # type: ignore[attr-defined]


def test_rotation_teleport_fails(tmp_path: Path) -> None:
    res = _run(tmp_path, _mesh(), _anim(rot_jump=True))
    assert res.checks["euler_continuity"] is False  # type: ignore[attr-defined]
