"""GoldSource sprites and weapon HUD files."""

from __future__ import annotations

import numpy as np
import pytest

from valve_qc_merger.sprite.hud import pack, parse_txt, sheets_of, weapon_txt
from valve_qc_merger.sprite.spr import SprError, from_images, quantize, read_spr, write_spr


def _icon(w: int = 40, h: int = 20) -> np.ndarray:
    image = np.zeros((h, w, 4), np.uint8)
    image[4:16, 5:35] = (255, 200, 40, 255)  # an opaque bar on a transparent field
    image[8:12, 10:30] = (40, 120, 255, 255)
    return image


def test_alphatest_round_trip_keeps_holes_and_colours() -> None:
    sprite = from_images([_icon(), _icon()], fmt="alphatest")
    again = read_spr(write_spr(sprite))
    assert (again.type_name, again.format_name, len(again.frames)) == ("parallel",
                                                                       "alphatest", 2)
    assert again.size == (40, 20)
    rgba = again.rgba(0)
    assert rgba[0, 0, 3] == 0  # a hole
    assert tuple(rgba[5, 6]) == (255, 200, 40, 255)
    assert tuple(rgba[9, 15, :3]) == (40, 120, 255)
    assert again.palette[255] == (0, 0, 255)


def test_indexalpha_and_additive() -> None:
    image = np.zeros((8, 8, 4), np.uint8)
    image[..., :3] = (200, 100, 50)
    image[..., 3] = np.arange(64, dtype=np.uint8).reshape(8, 8) * 4
    smoke = read_spr(write_spr(from_images([image], fmt="indexalpha")))
    assert smoke.format_name == "indexalpha"
    assert smoke.palette[255] == (200, 100, 50)
    assert smoke.rgba()[..., 3].tolist() == image[..., 3].tolist()
    glow = read_spr(write_spr(from_images([image], fmt="additive")))
    assert glow.rgba()[0, 0, :3].tolist() == [0, 0, 0]  # transparent became black


def test_quantize_many_colours() -> None:
    rng = np.random.default_rng(1)
    rgb = rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)
    palette, indices = quantize(rgb, 256)
    assert len(palette) <= 256 and indices.shape == (64, 64)
    error = np.abs(palette[indices].astype(int) - rgb.astype(int)).mean()
    assert error < 40


def test_broken_sprite() -> None:
    with pytest.raises(SprError):
        read_spr(b"NOPE" + b"\0" * 60)


def test_pack_icons_and_weapon_txt() -> None:
    sheets = pack({"ak": (170, 45), "ak_s": (170, 45), "m4": (170, 45), "ammo": (24, 24)})
    assert len(sheets) == 1 and set(sheets[0].rects) == {"ak", "ak_s", "m4", "ammo"}
    rects = list(sheets[0].rects.values())
    for i, a in enumerate(rects):  # no two icons overlap
        for b in rects[i + 1:]:
            assert (a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0]
                    or a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1])
    many = pack({f"w{i}": (170, 45) for i in range(12)})
    assert len(many) == 3  # five 45-pixel shelves per 256 sheet
    text = weapon_txt("weapon_balrog", "640hud_vqm1", sheets[0].rects["ak"],
                      sheets[0].rects["ak_s"], ("640hud_vqm1", *sheets[0].rects["ammo"]))
    entries = parse_txt(text)
    assert int(text.splitlines()[0]) == len(entries) == 8
    assert {e[0] for e in entries} == {"weapon", "weapon_s", "ammo", "crosshair", "autoaim"}
    assert sheets_of(text) == {"sprites/640hud_vqm1.spr", "sprites/crosshairs.spr"}


# -- project library, package, budget, studio --------------------------------
def _project_with_hud(tmp_path):  # noqa: ANN001, ANN202
    from valve_qc_merger.project import Project
    from valve_qc_merger.project import sprites as library
    project = Project.create(tmp_path / "pack")
    names = library.make_weapon_hud(project, "balrog9", _icon(200, 60),
                                    ammo=np.full((32, 32, 4), 200, np.uint8))
    library.make_sprite(project, "muzzle/flash1", [_icon()], fmt="additive")
    return project, names


def test_sprite_library_hud_and_effects(tmp_path) -> None:  # noqa: ANN001
    from valve_qc_merger.project import sprites as library
    project, names = _project_with_hud(tmp_path)
    assert names == ["weapon_balrog9.txt", "640hud_balrog9.spr"]
    assert library.list_sprites(project) == ["640hud_balrog9.spr", "muzzle/flash1.spr",
                                             "weapon_balrog9.txt"]
    assert library.hud_files(project) == {"sprites/weapon_balrog9.txt",
                                          "sprites/640hud_balrog9.spr"}
    assert library.effect_sprites(project) == ["sprites/muzzle/flash1.spr"]
    sheet = read_spr(library.sprite_path(project, "640hud_balrog9.spr"))
    assert sheet.format_name == "additive" and sheet.size[0] == 256
    imported = library.import_sprites(project, [library.sprite_path(project, names[0])])
    assert imported == ["weapon_balrog9.txt"]


def test_package_and_budget_count_sprites(tmp_path) -> None:  # noqa: ANN001
    from valve_qc_merger.server.budget import budget, project_load
    from valve_qc_merger.server.package import export_package
    project, _names = _project_with_hud(tmp_path)
    load = project_load(project)
    lines = budget(None, load)
    assert dict(lines["models"].parts)["project sprites"] == 1
    assert dict(lines["generic"].parts)["weapon HUD files"] == 2
    result = export_package(project, tmp_path / "out")
    assert result.sprites == ["sprites/muzzle/flash1.spr"]
    assert sorted(result.hud_files) == ["sprites/640hud_balrog9.spr",
                                        "sprites/weapon_balrog9.txt"]
    assert (tmp_path / "out" / "cstrike" / "sprites" / "muzzle" / "flash1.spr").is_file()
    inc = (tmp_path / "out" / "amxx" / "vqm_resources.inc").read_text()
    assert "precache_model(VQM_SPRITES[i]);" in inc
    assert "precache_generic(VQM_HUD_FILES[i]);" in inc


def test_sprites_in_the_studio(tmp_path) -> None:  # noqa: ANN001
    import os
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from PySide6.QtCore import QSettings

    from valve_qc_merger.studio.main_window import MainWindow
    project, _names = _project_with_hud(tmp_path)
    window = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    try:
        window.set_project(project)
        assert window.explorer.select("sprite", "muzzle/flash1.spr")
        panel = window.sprite_panel
        assert window.right.currentWidget() is panel
        assert panel.values["format"].text() == "additive"
        assert panel.values["size"].text() == "40×20"
        assert window.explorer.select("sprite", "weapon_balrog9.txt")
        assert panel.entries.rowCount() == 8 and not panel.entries.isHidden()
        assert panel.entries.cellWidget(0, 4).pixmap() is not None
    finally:
        window.close()
