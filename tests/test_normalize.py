"""Portable [A-Za-z0-9_] names for an imported model folder."""

from __future__ import annotations

import re
from pathlib import Path

from valve_qc_merger.project.normalize import normalize_model, portable
from valve_qc_merger.services.qc_check import check_qc

_SMD = ('version 1\nnodes\n  0 "Bip01 R Hand" -1\nend\nskeleton\ntime 0\n  0 0 0 0 0 0 0\nend\n'
        "triangles\n{mat}\n0 0 0 0 0 0 0 0 0\n0 0 0 0 0 0 0 0 0\n0 0 0 0 0 0 0 0 0\n"
        "{mat2}\n0 0 0 0 0 0 0 0 0\n0 0 0 0 0 0 0 0 0\n0 0 0 0 0 0 0 0 0\nend\n")


def test_portable_names() -> None:
    assert portable("äëÿ KakTycà") == "_KakTyc_"
    assert portable("Star - Glow") == "Star_Glow"
    assert portable("#256x[1]") == "_256x_1_"
    assert portable("") == "x"


def test_normalize_renames_files_and_rewrites_the_qc(tmp_path: Path) -> None:
    folder = tmp_path / "v_test"
    (folder / "maps_8bit").mkdir(parents=True)
    (folder / "anims dir").mkdir()
    for name in ("Star - Glow.bmp", "#128 red.BMP", "skin [2].bmp"):
        (folder / "maps_8bit" / name).write_bytes(b"BM")
    (folder / "Ref Mesh.smd").write_text(_SMD.format(mat="Star - Glow.bmp",
                                                     mat2="#128 red.BMP"), encoding="latin-1")
    (folder / "anims dir" / "äëÿ idle.smd").write_text(
        "version 1\nnodes\nend\nskeleton\nend\n", encoding="latin-1")
    (folder / "anims dir" / "Shoot-1.smd").write_text("version 1\nnodes\nend\n",
                                                      encoding="latin-1")
    qc = folder / "v_test.qc"
    qc.write_text(
        '$modelname "v_test.mdl"\n$cd "."\n$cdtexture "./maps_8bit"\n'
        '$texrendermode "Star - Glow.bmp" additive\n$texrendermode \"#128 red.BMP\" masked\n'
        '$bodygroup "my hands"\n{\n studio "Ref Mesh"\n blank\n}\n'
        '$texturegroup skinfamilies\n{\n { "Star - Glow.bmp" }\n { "skin [2].bmp" }\n}\n'
        '$sequence "äëÿ idle" {\n "anims dir/äëÿ idle"\n fps 30\n'
        ' { event 5004 1 "weapons/x y.wav" }\n}\n'
        '$sequence "shoot 1" "anims dir/Shoot-1" fps 20 LX\n', encoding="latin-1")

    report = normalize_model(folder)
    text = qc.read_text(encoding="latin-1")
    assert '$sequence "_idle"' in text and '"anims_dir/_idle"' in text
    assert '$sequence "shoot_1" "anims_dir/Shoot_1"' in text
    assert '$bodygroup "my_hands"' in text and 'studio "Ref_Mesh"' in text
    assert '$texrendermode "Star_Glow.bmp" additive' in text
    assert '$texrendermode "_128_red.bmp" masked' in text
    assert '{ "skin_2_.bmp" }' in text
    assert '"weapons/x y.wav"' in text  # sound paths are the game's, not ours
    smd = (folder / "Ref_Mesh.smd").read_text(encoding="latin-1")
    assert "Star_Glow.bmp" in smd and "_128_red.bmp" in smd and "Bip01 R Hand" in smd
    files = [p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()]
    assert all(re.fullmatch(r"[A-Za-z0-9_/]+\.(qc|smd|bmp)", f) for f in files), files
    assert not [p for p in check_qc(qc) if p.level == "error"]
    assert report.renamed["sequence: shoot 1"] == "shoot_1"
    assert normalize_model(folder).renamed == {}  # a second pass changes nothing
