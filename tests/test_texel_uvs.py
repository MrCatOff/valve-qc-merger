"""Decompiled UVs studiomdl turns back into the very same texels."""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from valve_qc_merger.mdl.decompile import _texel_uv
from valve_qc_merger.project.texel_uvs import fix_directory


def _studiomdl_texel(u: float, v: float, width: int, height: int) -> tuple[int, int]:
    # studiomdl (TextureCoordRanges): s = int(u * (w - 1)), t from int(v * (h - 1))
    # flipped against the range's top; floats as it parses them from the SMD
    s = int(np.float32(float(f"{u:.6f}")) * (width - 1))
    t_up = int(np.float32(float(f"{v:.6f}")) * (height - 1))
    return s, (height - 1) - t_up


def test_every_texel_survives_studiomdl() -> None:
    for width, height in ((512, 512), (256, 128), (128, 128), (252, 123), (8, 8)):
        for s in range(width):
            for t in (0, 1, height // 2, height - 2, height - 1):
                uv = _texel_uv(s, t, width, height)
                assert _studiomdl_texel(uv.u, uv.v, width, height) == (s, t)


def _bmp(path: Path, width: int, height: int) -> None:
    header = b"BM" + struct.pack("<IHHI", 0, 0, 0, 54) + struct.pack("<iii", 40, width, height)
    path.write_bytes(header + b"\0" * 32)


def _smd(path: Path, rows: list[tuple[str, float, float]]) -> None:
    lines = ["version 1", "nodes", '0 "root" -1', "end", "skeleton", "time 0",
             "0 0 0 0 0 0 0", "end", "triangles"]
    for i in range(0, len(rows), 3):
        lines.append(rows[i][0])
        lines += [f"  0 0.000000 0.000000 0.000000 0.000000 0.000000 1.000000 "
                  f"{u:.6f} {v:.6f}" for _m, u, v in rows[i:i + 3]]
    lines.append("end")
    path.write_text("\n".join(lines) + "\n", encoding="latin-1")


def test_old_decompiles_are_fixed_once(tmp_path: Path) -> None:
    _bmp(tmp_path / "skin.bmp", 256, 128)
    old = [("skin.bmp", s / 256, 1 - t / 128) for s, t in ((0, 0), (255, 127), (100, 64))]
    _smd(tmp_path / "reference.smd", old)
    other = [("skin.bmp", u, v) for u, v in ((0.1234567, 0.5), (0.9, 0.31), (0.5, 0.5))]
    _smd(tmp_path / "artist.smd", other)  # another tool's: not whole texels
    assert fix_directory(tmp_path) == 1
    fixed = [line.split() for line in (tmp_path / "reference.smd").read_text().splitlines()
             if line.startswith("  0 ")]
    texels = [_studiomdl_texel(float(f[7]), float(f[8]), 256, 128) for f in fixed]
    assert texels == [(0, 0), (255, 127), (100, 64)]
    assert (tmp_path / "artist.smd").read_text().count("0.123457") == 1  # untouched
    assert fix_directory(tmp_path) == 0  # fixed already: left alone


def test_texel_edges_of_other_decompilers_are_centred(tmp_path: Path) -> None:
    # u = s / (w - 1): on studiomdl's scale but on the texel's edge — after
    # 6 decimals about half truncate a texel short; centred, none do
    _bmp(tmp_path / "hands.bmp", 512, 512)
    wanted = [(s, t) for s in (1, 77, 300, 511) for t in (0, 3, 400)][:12]
    rows = [("hands.bmp", s / 511, (511 - t) / 511) for s, t in wanted]
    _smd(tmp_path / "hands.smd", rows)
    assert fix_directory(tmp_path) == 1
    fixed = [line.split() for line in (tmp_path / "hands.smd").read_text().splitlines()
             if line.startswith("  0 ")]
    assert [_studiomdl_texel(float(f[7]), float(f[8]), 512, 512) for f in fixed] == wanted


def test_opening_an_older_project_fixes_its_assets(tmp_path: Path) -> None:
    from valve_qc_merger.project import Project
    from valve_qc_merger.project.model import PROJECT_FILE
    source = tmp_path / "v_old"
    source.mkdir()
    _bmp(source / "skin.bmp", 256, 128)
    _smd(source / "reference.smd", [("skin.bmp", s / 256, 1 - t / 128)
                                    for s, t in ((0, 0), (255, 127), (100, 64))])
    (source / "v_old.qc").write_text('$modelname v_old.mdl\n$body "studio" "reference"\n')
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(source)  # (imports fix it too)
    smd = project.asset_dir("v_old") / "reference.smd"
    _smd(smd, [("skin.bmp", s / 256, 1 - t / 128) for s, t in ((0, 0), (255, 127), (100, 64))])
    toml = project.root / PROJECT_FILE
    toml.write_text(toml.read_text().replace("format = 3", "format = 2"))
    Project.open(project.root)
    fixed = [line.split() for line in smd.read_text().splitlines() if line.startswith("  0 ")]
    assert [_studiomdl_texel(float(f[7]), float(f[8]), 256, 128) for f in fixed] == \
        [(0, 0), (255, 127), (100, 64)]
    assert "format = 3" in toml.read_text()
