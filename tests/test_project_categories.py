"""Asset categories: filing, the v_/p_/w_ weapon link, category builds."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from valve_qc_merger.project import Asset, Build, Project, ProjectError, weapon_key


def _model(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / f"{name}.qc").write_text(f'$modelname "{name}.mdl"\n')
    return directory


@pytest.fixture()
def project(tmp_path: Path) -> Project:
    src = tmp_path / "src"
    for name in ("v_deagle", "p_deagle", "w_deagle", "v_mp5", "p_mp5", "v_m4a1"):
        _model(src, name)
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(src)
    return project


def test_weapon_key() -> None:
    assert weapon_key("v_Deagle") == weapon_key("p_deagle") == weapon_key("w_deagle")
    assert weapon_key("player_x") == "player_x"


def test_move_brings_the_weapon_along(project: Project) -> None:
    assert project.siblings("v_deagle") == ["p_deagle", "w_deagle"]
    moved = project.set_category(["v_deagle"], "Pistols")
    assert sorted(moved) == ["p_deagle", "v_deagle", "w_deagle"]
    assert project.categories == ["Pistols"]
    alone = project.set_category(["v_mp5"], "SMG", with_siblings=False)
    assert alone == ["v_mp5"] and project.assets["p_mp5"].category == ""
    # a category name is matched case-insensitively
    project.set_category(["v_m4a1"], "pistols")
    assert project.assets["v_m4a1"].category == "Pistols"
    with pytest.raises(ProjectError):
        project.set_category(["v_deagle"], "a/b")


def test_imports_follow_the_weapon_and_derived_assets_inherit(project: Project,
                                                              tmp_path: Path) -> None:
    project.set_category(["v_mp5"], "SMG")
    _model(tmp_path / "more", "w_mp5")
    _model(tmp_path / "more", "v_p90")
    project.import_decompiled(tmp_path / "more")
    assert project.assets["w_mp5"].category == "SMG"  # followed v_mp5
    assert project.assets["v_p90"].category == ""
    _model(tmp_path / "late", "v_ak47")
    (added,) = project.import_decompiled(tmp_path / "late" / "v_ak47", category="Rifles")
    assert added.category == "Rifles" and "Rifles" in project.categories
    # derived assets belong to the weapon of their source
    project.assets["v_mp5_hands"] = Asset(
        name="v_mp5_hands", kind="v", path="assets/v/v_mp5_hands",
        derived={"from": "v_mp5", "mode": "hands", "options": {}}, category="SMG")
    assert project.weapon_of("v_mp5_hands") == "mp5"
    assert "v_mp5_hands" in project.siblings("w_mp5")


def test_rename_delete_and_round_trip(project: Project) -> None:
    project.set_category(["v_deagle"], "Pistols")
    project.add_category("Empty")
    project.add_build(Build("pistols_v", "merge-v", category="Pistols"))
    project.rename_category("Pistols", "Handguns")
    assert project.builds["pistols_v"].category == "Handguns"
    assert project.assets["w_deagle"].category == "Handguns"
    reopened = Project.open(project.root)
    assert sorted(reopened.categories) == ["Empty", "Handguns"]  # empty ones persist
    assert reopened.assets["p_deagle"].category == "Handguns"
    assert reopened.builds["pistols_v"].category == "Handguns"
    reopened.remove_category("Handguns")
    assert reopened.assets["p_deagle"].category == ""
    assert reopened.builds["pistols_v"].category is None
    with pytest.raises(ProjectError):
        reopened.rename_category("nope", "x")
    with pytest.raises(ProjectError):
        reopened.rename_category("Empty", "a/b")


def test_category_builds(project: Project) -> None:
    project.set_category(["v_deagle", "v_m4a1"], "Pistols")
    made = project.create_category_builds("Pistols")
    assert {(b.name, b.kind) for b in made} == {
        ("pistols_v", "merge-v"), ("pistols_p", "merge-p"), ("pistols_w", "merge-w")}
    view = project.builds["pistols_v"]
    assert view.retarget and view.options == {"name": "v_pistols"}
    assert [a.name for a in project.build_assets(view)] == ["v_deagle", "v_m4a1"]
    assert [a.name for a in project.build_assets(project.builds["pistols_w"])] == ["w_deagle"]
    assert project.create_category_builds("Pistols") == []  # existing names kept
    with pytest.raises(ProjectError):
        project.add_build(Build("both", "merge-v", assets=["v_mp5"], category="Pistols"))


def test_format_1_projects_still_open(tmp_path: Path) -> None:
    root = tmp_path / "old"
    root.mkdir()
    (root / "project.toml").write_text(
        '[project]\nname = "old"\nformat = 1\n\n[[assets]]\nname = "v_x"\nkind = "v"\n'
        'path = "assets/v/v_x"\nnotes = ""\n')
    project = Project.open(root)
    assert project.assets["v_x"].category == "" and project.categories == []


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture()
def window(tmp_path: Path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings

    from valve_qc_merger.studio.main_window import MainWindow
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    yield win
    win.close()


def _tree(win) -> list[str]:
    lines: list[str] = []

    def walk(item, depth: int) -> None:
        lines.append("  " * depth + item.text(0))
        for i in range(item.childCount()):
            walk(item.child(i), depth + 1)
    for i in range(win.explorer.topLevelItemCount()):
        walk(win.explorer.topLevelItem(i), 0)
    return lines


def test_explorer_groups_by_category_and_moves(window, project: Project,
                                               monkeypatch) -> None:
    window.set_project(project)
    assert "  View models (v_)  ·  3" in _tree(window)  # no categories: flat as before
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    window.move_to_category(["p_deagle"], "Pistols")
    assert project.assets["v_deagle"].category == "Pistols"  # the weapon came along
    tree = _tree(window)
    assert "  Pistols  ·  3" in tree and "  Uncategorized  ·  3" in tree
    assert "      v_deagle" in tree
    assert window.explorer.select("asset", "w_deagle")
    assert window.explorer.current_category() == "Pistols"
    assert window.inspector.category_label.text() == "Pistols"

    window.create_category_builds("Pistols")
    assert window.explorer.current_build() == "pistols_v"
    panel = window.build_panel
    assert panel.category_radio.isChecked() and panel.category_combo.currentText() == "Pistols"
    panel.all_radio.setChecked(True)
    assert panel.save() and project.builds["pistols_v"].category is None
    panel.show_build(project, "pistols_v")
    panel.category_radio.setChecked(True)
    assert panel.save() and project.builds["pistols_v"].category == "Pistols"


def test_derived_assets_nest_under_their_source(window, project: Project) -> None:
    for name, mode in (("v_deagle_hands", "hands"), ("v_deagle_canon", "canon")):
        project.assets[name] = Asset(name=name, kind="v", path=f"assets/v/{name}",
                                     derived={"from": "v_deagle", "mode": mode,
                                              "options": {}})
    project.assets["v_mp5_hands"] = Asset(name="v_mp5_hands", kind="v",
                                          path="assets/v/v_mp5_hands", category="SMG",
                                          derived={"from": "v_mp5", "mode": "hands",
                                                   "options": {}})
    project.categories.append("SMG")
    window.set_project(project)
    tree = _tree(window)
    deagle = tree.index("      v_deagle")
    assert tree[deagle + 1:deagle + 3] == ["        v_deagle_canon  ·  canon",
                                           "        v_deagle_hands  ·  hands"]
    # the source sits in another category: shown flat, marked with its source
    assert "      v_mp5_hands  ↳ v_mp5" in tree
    assert window.explorer.select("asset", "v_deagle_hands")
    assert window.explorer.current_asset() == "v_deagle_hands"


def test_explorer_paints_connectors(window, project: Project) -> None:
    project.set_category(["v_deagle"], "Pistols")
    window.set_project(project)
    window.explorer.resize(300, 400)
    image = window.explorer.grab().toImage()  # drawBranches runs for every row
    assert not image.isNull() and image.width() == window.explorer.width()


def _visible(win) -> list[str]:
    lines: list[str] = []

    def walk(item, depth: int) -> None:
        if item.isHidden():
            return
        lines.append("  " * depth + item.text(0))
        for i in range(item.childCount()):
            walk(item.child(i), depth + 1)
    for i in range(win.explorer.topLevelItemCount()):
        walk(win.explorer.topLevelItem(i), 0)
    return lines


def test_explorer_filter(window, project: Project) -> None:
    project.set_category(["v_deagle"], "Pistols")
    project.add_build(Build("deagle_pack", "merge-v", category="Pistols"))
    project.assets["v_mp5_hands"] = Asset(name="v_mp5_hands", kind="v",
                                          path="assets/v/v_mp5_hands",
                                          derived={"from": "v_mp5", "mode": "hands",
                                                   "options": {}})
    window.set_project(project)
    panel = window.explorer_panel
    panel.search.setText("DEAGLE")  # case-insensitive, builds too
    shown = _visible(window)
    assert "      v_deagle" in shown and "      w_deagle" in shown
    assert not any("mp5" in line for line in shown)
    assert not any("Uncategorized" in line for line in shown)  # nothing inside
    assert "  deagle_pack  ·  merge-v" in shown
    panel.search.setText("hands")  # a derived child keeps its parent visible
    shown = _visible(window)
    assert "      v_mp5" in shown and "        v_mp5_hands  ·  hands" in shown
    assert not any("v_m4a1" in line for line in shown)
    panel.search.clear()
    panel.status_box.setCurrentIndex(panel.status_box.findData("derived"))
    shown = _visible(window)
    assert any("v_mp5_hands" in line for line in shown)
    assert not any("deagle_pack" in line for line in shown)  # builds hide on status
    window.explorer.show_project(project)  # a refresh keeps the filter
    assert not any("w_deagle" in line for line in _visible(window))
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    panel.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                  Qt.KeyboardModifier.NoModifier))
    assert not window.explorer.filtering and "      w_deagle" in _visible(window)
