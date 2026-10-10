"""merge-players finds a body's textures in a texture folder too."""

from __future__ import annotations

from pathlib import Path

from valve_qc_merger.merge_players.discovery import _resolve_materials
from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import Node, Smd, Triangle, Vertex


def test_textures_in_maps_8bit_are_found(tmp_path: Path) -> None:
    (tmp_path / "maps_8bit").mkdir()
    (tmp_path / "maps_8bit" / "Body.bmp").write_bytes(b"BM")
    v = Vertex(0, Vector3(0, 0, 0), Vector3(0, 0, 1), Vector2(0, 0))
    mesh = Smd(nodes=[Node(0, "root", -1)], frames=[],
               triangles=[Triangle("body.bmp", (v, v, v))])
    assert _resolve_materials([mesh], tmp_path) == []
    assert mesh.triangles[0].material == "Body.bmp"  # the name on disk
