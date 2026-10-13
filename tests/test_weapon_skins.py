"""A weapon's p_ and w_ models get as many skins as its v_ model."""

from __future__ import annotations

import shutil
from pathlib import Path

from valve_qc_merger.merge_player.merger import parse_texturegroups
from valve_qc_merger.project import Project
from valve_qc_merger.project.weapon_skins import align_staged_skins, aligned_rows

_SMD = ('version 1\nnodes\n0 "root" -1\nend\nskeleton\ntime 0\n0 0 0 0 0 0 0\nend\n'
        "triangles\n{texture}\n  0 0 0 0 0 0 1 0 0\n  0 1 0 0 0 0 1 1 0\n  0 0 1 0 0 0 1 0 1\n"
        "end\n")


def _model(root: Path, name: str, textures: list[str], rows: list[list[str]]) -> Path:
    folder = root / name
    (folder / "maps_8bit").mkdir(parents=True)
    for texture in textures:
        (folder / "maps_8bit" / texture).write_bytes(b"BM" + texture.encode())
    (folder / "ref.smd").write_text(_SMD.format(texture=textures[0]))
    group = ("$texturegroup skinfamilies\n{\n"
             + "".join("    { " + " ".join(f'"{t}"' for t in r) + " }\n" for r in rows)
             + "}\n\n") if rows else ""
    (folder / f"{name}.qc").write_text(f"$modelname {name}.mdl\n$cdtexture ./maps_8bit\n"
                                       f'$body "studio" "ref"\n\n{group}'
                                       '$sequence "idle" "ref"\n')
    return folder


def test_rows_come_from_the_sibling_then_repeat_the_first() -> None:
    sibling = [["gun_blue.bmp", "bag_blue.bmp"], ["gun_yellow.bmp", "bag_yellow.bmp"]]
    rows, taken = aligned_rows([], {"gun_blue.bmp", "bag_blue.bmp"}, sibling, 3, "gun_blue.bmp")
    assert rows == sibling + [sibling[0]] and taken == 1  # the yellow, then the blue again
    rows, taken = aligned_rows([], {"other.bmp"}, sibling, 3, "other.bmp")
    assert rows == [["other.bmp"]] * 3 and taken == 0  # not its textures: first row only


def test_staged_p_and_w_models_get_the_view_models_skins(tmp_path: Path) -> None:
    source = tmp_path / "src"
    _model(source, "v_gun", ["gun_v.bmp", "gun_v_red.bmp", "gun_v_gold.bmp"],
           [["gun_v.bmp"], ["gun_v_red.bmp"], ["gun_v_gold.bmp"]])
    _model(source, "p_gun", ["gun_p.bmp", "gun_p_red.bmp"], [["gun_p.bmp"], ["gun_p_red.bmp"]])
    _model(source, "w_gun", ["gun_p.bmp"], [])
    project = Project.create(tmp_path / "pack")
    for name in ("v_gun", "p_gun", "w_gun"):
        project.import_decompiled(source / name)
    staged = tmp_path / "staged"
    for name in ("p_gun", "w_gun"):
        shutil.copytree(project.asset_dir(name), staged / name)
    notes = align_staged_skins(project, staged, [project.assets["p_gun"],
                                                 project.assets["w_gun"]])
    assert len(notes) == 2
    p_rows = parse_texturegroups((staged / "p_gun" / "p_gun.qc").read_text())
    w_rows = parse_texturegroups((staged / "w_gun" / "w_gun.qc").read_text())
    assert p_rows == [["gun_p.bmp"], ["gun_p_red.bmp"], ["gun_p.bmp"]]  # 3, as the v_
    assert w_rows == p_rows  # the red from p_gun, then its first again
    assert (staged / "w_gun" / "maps_8bit" / "gun_p_red.bmp").is_file()  # brought along
    original = parse_texturegroups(next(project.asset_dir("w_gun").glob("*.qc")).read_text())
    assert original == []  # the asset itself is untouched
