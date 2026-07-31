"""Texture finalisation tests (delivery contract), no Blender."""

from __future__ import annotations

import struct
from pathlib import Path

from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget.textures import finalize_textures
from valve_qc_merger.writers.smd import write_smd_file


def _bmp(path: Path, bits: int = 8) -> None:
    header = bytearray(54)
    header[0:2] = b"BM"
    struct.pack_into("<H", header, 28, bits)
    path.write_bytes(bytes(header))


def _mesh_smd(path: Path, material: str) -> None:
    nodes = [Node(0, "root", -1)]
    tri = Triangle(material, tuple(  # type: ignore[arg-type]
        Vertex(0, Vector3(i, 0, 0), Vector3(0, 0, 1), Vector2(0, 0)) for i in range(3)
    ))
    smd = Smd(nodes=nodes,
              frames=[Frame(0, (BonePose(0, Vector3(0, 0, 0), Vector3(0, 0, 0)),))],
              triangles=[tri])
    write_smd_file(smd, path)


def test_missing_extension_is_appended_and_texture_staged(tmp_path: Path) -> None:
    src_dir = tmp_path / "in"
    out = tmp_path / "out"
    src_dir.mkdir(), out.mkdir()
    _bmp(src_dir / "gun.bmp")
    mesh = out / "weapon.smd"
    _mesh_smd(mesh, "gun")  # extensionless material

    report = finalize_textures(out, {"weapon": mesh}, [src_dir])
    assert not report.errors
    assert (out / "gun.bmp").exists()
    assert {t.material for t in parse_smd_file(mesh).triangles} == {"gun.bmp"}


def test_spaces_are_sanitised(tmp_path: Path) -> None:
    src_dir = tmp_path / "in"
    out = tmp_path / "out"
    src_dir.mkdir(), out.mkdir()
    _bmp(src_dir / "my tex.bmp")
    mesh = out / "weapon.smd"
    _mesh_smd(mesh, "my tex.bmp")

    report = finalize_textures(out, {"weapon": mesh}, [src_dir])
    assert not report.errors
    assert (out / "my_tex.bmp").exists()
    assert {t.material for t in parse_smd_file(mesh).triangles} == {"my_tex.bmp"}


def test_non_8bit_bmp_and_missing_texture_fail(tmp_path: Path) -> None:
    src_dir = tmp_path / "in"
    out = tmp_path / "out"
    src_dir.mkdir(), out.mkdir()
    _bmp(src_dir / "deep.bmp", bits=24)
    a = out / "a.smd"
    b = out / "b.smd"
    _mesh_smd(a, "deep.bmp")
    _mesh_smd(b, "absent.bmp")

    report = finalize_textures(out, {"a": a, "b": b}, [src_dir])
    assert any("24-bit" in e for e in report.errors)
    assert any("not found" in e for e in report.errors)
