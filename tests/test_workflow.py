"""Project templates and importing a whole server folder."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest

from valve_qc_merger.project import Project
from valve_qc_merger.project.workflow import TEMPLATES, apply_template, import_server_folder
from valve_qc_merger.sound.wav import write_wav

_MINI = Path(__file__).parent / "examples" / "mdl" / "mini.mdl"


def test_templates(tmp_path: Path) -> None:
    classic = Project.create(tmp_path / "classic")
    apply_template(classic, "classic")
    assert "pistols" in classic.categories and "knives" in classic.categories
    assert not classic.builds
    zombie = Project.create(tmp_path / "zombie")
    apply_template(zombie, "zombie")
    assert sorted(zombie.builds) == ["weapons_player", "weapons_view", "weapons_world",
                                     "zombie_hands"]
    assert zombie.builds["weapons_view"].retarget
    assert Project.open(zombie.root).builds["zombie_hands"].category == "zombie hands"
    empty = Project.create(tmp_path / "empty")
    apply_template(empty, "empty")
    assert not empty.categories and set(TEMPLATES) == {"empty", "classic", "zombie"}
    with pytest.raises(ValueError):
        apply_template(empty, "nope")


def _server(tmp_path: Path) -> Path:
    mod = tmp_path / "cstrike"
    (mod / "models" / "weapons").mkdir(parents=True)
    shutil.copy(_MINI, mod / "models" / "weapons" / "v_mini.mdl")  # plays weapons/x.wav
    for name in ("weapons/x.wav", "ambience/unused.wav"):
        path = mod / "sound" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(write_wav(np.zeros((50, 1)), 22050))
    sprites = mod / "sprites"
    sprites.mkdir()
    (sprites / "weapon_mini.txt").write_text(
        "2\nweapon 640 640hud_mini 0 0 170 45\nweapon_s 640 640hud_mini 0 0 170 45\n")
    (sprites / "640hud_mini.spr").write_bytes(b"IDSP")
    (sprites / "unrelated.spr").write_bytes(b"IDSP")
    return mod


def test_import_server_folder(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    mod = _server(tmp_path)
    result = import_server_folder(project, mod)
    assert result.models == ["v_mini"] and not result.failed
    assert result.sounds == ["weapons/x.wav"]  # only what the models play
    assert sorted(result.sprites) == ["640hud_mini.spr", "weapon_mini.txt"]
    again = import_server_folder(project, mod, sounds="all")
    assert again.skipped == ["v_mini"] and not again.models
    assert sorted(again.sounds) == ["ambience/unused.wav", "weapons/x.wav"]


def test_import_server_folder_from_the_studio(tmp_path: Path) -> None:
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from PySide6.QtCore import QSettings

    from valve_qc_merger.studio import dialogs
    from valve_qc_merger.studio.main_window import MainWindow
    project = Project.create(tmp_path / "pack")
    project.settings.game_dir = str(_server(tmp_path))
    window = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    try:
        window.set_project(project)
        original = dialogs.ImportServerDialog.exec
        dialogs.ImportServerDialog.exec = lambda self: dialogs.ImportServerDialog.DialogCode \
            .Accepted
        try:
            window.import_server_folder()
        finally:
            dialogs.ImportServerDialog.exec = original
        assert window.jobs.wait(120_000)
        assert "v_mini" in project.assets
        assert "imported 1 model(s), 1 sound(s), 2 sprite file(s)" in window.log.toPlainText()
    finally:
        window.close()


def test_new_project_dialog_offers_templates(tmp_path: Path) -> None:
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from valve_qc_merger.studio.dialogs import NewProjectDialog
    dialog = NewProjectDialog(None, tmp_path)
    assert dialog.template() == "empty"
    dialog.template_box.setCurrentIndex(dialog.template_box.findData("zombie"))
    assert dialog.template() == "zombie" and "zombie-hands" in dialog.template_hint.text()
