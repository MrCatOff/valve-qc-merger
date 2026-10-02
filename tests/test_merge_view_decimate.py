"""Folding a multi-part weapon into one submodel (half-edge decimation)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from valve_qc_merger.merge_view.bodygroups import FOLDED_STEM, collapse_bodygroups
from valve_qc_merger.merge_view.decimate import (
    concat_by_name,
    decimate,
    fold_parts,
    surface_distance,
    unique_vertices,
)
from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex

ZERO = Vector3(0.0, 0.0, 0.0)


def _grid(n: int, *, bone_of=lambda i, j: 0, z=lambda x, y: 0.0, x0=0.0,
          material="tex.bmp", nodes=None) -> Smd:
    """An n x n vertex sheet (one UV island), two triangles per cell."""
    def vert(i, j):
        x, y = x0 + i, float(j)
        return Vertex(bone=bone_of(i, j), position=Vector3(x, y, z(x, y)),
                      normal=Vector3(0.0, 0.0, 1.0), uv=Vector2(i / (n - 1), j / (n - 1)))
    tris = []
    for i in range(n - 1):
        for j in range(n - 1):
            a, b, c, d = vert(i, j), vert(i + 1, j), vert(i + 1, j + 1), vert(i, j + 1)
            tris += [Triangle(material, (a, b, c)), Triangle(material, (a, c, d))]
    nodes = nodes or [Node(0, "root", -1), Node(1, "gun", 0)]
    return Smd(nodes=nodes, frames=[Frame(0, tuple(BonePose(n.index, ZERO, ZERO)
                                                   for n in nodes))], triangles=tris)


def _normals(smd: Smd) -> np.ndarray:
    out = []
    for t in smd.triangles:
        p = [np.array(tuple(v.position)) for v in t.vertices]
        out.append(np.cross(p[1] - p[0], p[2] - p[0]))
    return np.array(out)


def test_decimate_reaches_the_target_without_new_positions_or_flips() -> None:
    mesh = _grid(12)  # 144 vertices, flat
    before = {tuple(v.position) for t in mesh.triangles for v in t.vertices}
    result = decimate(mesh, target=100, max_fraction=0.5)
    assert result is not None
    out, _shift, removed = result
    assert unique_vertices(out) <= 100 and len(removed) == 44
    after = {tuple(v.position) for t in out.triangles for v in t.vertices}
    assert after <= before  # half-edge collapses only: no new positions
    assert (_normals(out)[:, 2] > 0).all()  # nothing flipped
    # the sheet's border is fixed: its outline is intact
    border = {p for p in before if p[0] in (0.0, 11.0) or p[1] in (0.0, 11.0)}
    assert border <= after
    assert surface_distance(removed, out) < 1e-9  # flat sheet: no shape change


def test_decimate_keeps_bones_apart_and_respects_the_limit() -> None:
    mesh = _grid(10, bone_of=lambda i, j: 0 if i < 5 else 1)
    out, _s, _r = decimate(mesh, target=80, max_fraction=0.5)
    # no vertex changed bone: every position keeps the bone it had
    bone_at = {tuple(v.position): v.bone for t in mesh.triangles for v in t.vertices}
    assert all(bone_at[tuple(v.position)] == v.bone for t in out.triangles for v in t.vertices)
    assert decimate(mesh, target=10, max_fraction=0.2) is None  # needs 90%


def test_fold_parts_joins_parts_by_bone_name() -> None:
    a = _grid(6, bone_of=lambda i, j: 1)
    # same bones, another node order: vertices must be rebound by NAME
    b = _grid(6, x0=20.0, bone_of=lambda i, j: 0,
              nodes=[Node(0, "gun", 1), Node(1, "root", -1)])
    combined = concat_by_name([a, b])
    assert all(v.bone == 1 for t in combined.triangles for v in t.vertices)
    folded, report = fold_parts([a, b], budget=60, max_fraction=0.3)
    assert report.parts == 2 and report.vertices_before == 72
    assert report.vertices_after <= 60 and unique_vertices(folded) <= 60
    assert fold_parts([a, b], budget=30, max_fraction=0.3) is None


def test_collapse_folds_multipart_weapons_only_when_allowed(tmp_path: Path) -> None:
    big = _grid(40)  # 1600 vertices
    other = _grid(18, x0=100.0)  # 324 -> 1924: fits, packed into one group anyway
    third = _grid(16, x0=200.0)  # 256 -> 2180 > 2048
    model = ModelInput(name="v_x", directory=tmp_path, qc_path=tmp_path / "v_x.qc",
                       qc_text="", sequences=[],
                       bodygroups={"weapon": ["a"], "weapon_2": ["b"], "weapon_3": ["c"],
                                   "hands": ["h"]},
                       meshes={"a": big, "b": other, "c": third,
                               "h": _grid(3, bone_of=lambda i, j: 0)})
    plain = collapse_bodygroups(model)
    assert len(plain.weapon_stems) == 2 and not plain.synthetic
    folded = collapse_bodygroups(model, max_decimation=0.15)
    assert folded.weapon_stems == [[FOLDED_STEM]] and folded.synthetic == {FOLDED_STEM}
    assert unique_vertices(model.meshes[FOLDED_STEM]) <= 2048
    assert any("folded 3 parts" in w for w in folded.warnings)
