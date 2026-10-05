"""Batch imports that skip and go on, prop filtering, the Explorer keeping
its state, category retargets, maps outside the game folder in Budget."""

from __future__ import annotations

import os
import shutil
import struct
from pathlib import Path

import pytest

from tests.test_server import ENTITIES, write_bsp
from valve_qc_merger.project import Project
from valve_qc_merger.project.model import model_role

_MINI = Path(__file__).parent / "examples" / "mdl" / "mini.mdl"
_EXAMPLES = Path(__file__).parent / "examples"


def _with_sequences(source: bytes, labels: list[str]) -> bytes:
    """``source`` with its sequence labels replaced (count kept)."""
    data = bytearray(source)
    count, index = struct.unpack_from("<2i", data, 136 + 7 * 4)
    for i in range(min(count, len(labels))):
        data[index + i * 176:index + i * 176 + 32] = labels[i].encode().ljust(32, b"\0")
    return bytes(data)


def test_model_role(tmp_path: Path) -> None:
    (tmp_path / "models" / "player" / "gign").mkdir(parents=True)
    weapon = tmp_path / "models" / "v_gun.mdl"
    weapon.write_bytes(_MINI.read_bytes())
    prop = tmp_path / "models" / "crate.mdl"
    prop.write_bytes(_MINI.read_bytes())
    folder_player = tmp_path / "models" / "player" / "gign" / "gign.mdl"
    folder_player.write_bytes(_MINI.read_bytes())
    aiming = tmp_path / "models" / "zombie.mdl"
    aiming.write_bytes(_with_sequences(_MINI.read_bytes(), ["ref_aim_knife"]))
    assert [model_role(p) for p in (weapon, prop, folder_player, aiming)] == \
        ["weapon", "other", "player", "player"]


def test_import_models_skips_ignores_and_goes_on(tmp_path: Path) -> None:
    folder = tmp_path / "server" / "models"
    folder.mkdir(parents=True)
    shutil.copy(_MINI, folder / "v_one.mdl")
    shutil.copy(_MINI, folder / "crate.mdl")  # a prop: left out of a folder import
    (folder / "v_broken.mdl").write_bytes(b"IDST" + b"\0" * 10)
    project = Project.create(tmp_path / "pack")
    first = project.import_models([folder])
    assert [a.name for a in first.added] == ["v_one"]
    assert first.ignored == ["crate"] and len(first.failed) == 1
    assert first.failed[0].startswith("v_broken.mdl:")
    again = project.import_models([folder])  # import everything again: no error
    assert again.skipped == ["v_one"] and not again.added
    chosen = project.import_models([folder / "crate.mdl"])  # named on its own: imported
    assert [a.name for a in chosen.added] == ["crate"]


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def window(tmp_path: Path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from PySide6.QtCore import QSettings

    from valve_qc_merger.studio.main_window import MainWindow
    win = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    yield win
    win.close()


def test_explorer_keeps_collapsed_branches_and_selection(window, tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_EXAMPLES / "v_anaconda")
    project.import_decompiled(_EXAMPLES / "pair_deagle" / "v_deagle")
    window.set_project(project)
    tree = window.explorer
    from valve_qc_merger.studio.widgets import ROLE_KIND
    tree.select("asset", "v_deagle")
    sounds = next(i for i in tree._walk() if i.data(0, ROLE_KIND) == "group")
    sounds.setExpanded(False)
    seen: list[str] = []
    tree.asset_selected.connect(seen.append)
    tree.show_project(project)  # e.g. after a job finished
    sounds = next(i for i in tree._walk() if i.data(0, ROLE_KIND) == "group")
    assert not sounds.isExpanded()  # stays collapsed
    assert tree.current_asset() == "v_deagle" and seen == []  # no reselect / reload
    project.import_decompiled(_EXAMPLES / "pair_deagle" / "v_g_deagle")
    tree.show_project(project)  # a new asset appears; the rest keeps its state
    assert tree.current_asset() == "v_deagle"
    assert tree.select("asset", "v_g_deagle")


def test_category_retarget_menu_names(window, tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_EXAMPLES / "v_anaconda", category="pistols")
    project.import_decompiled(_EXAMPLES / "pair_deagle" / "v_deagle", category="pistols")
    window.set_project(project)
    tree = window.explorer
    from valve_qc_merger.studio.widgets import ROLE_KIND
    category = next(i for i in tree._walk() if i.data(0, ROLE_KIND) == "category")
    assert tree.retargetable(tree.assets_under(category)) == ["v_anaconda", "v_deagle"]
    project.assets["v_anaconda"].derived = None
    from valve_qc_merger.project.model import Asset
    project.assets["v_anaconda_hands"] = Asset(
        "v_anaconda_hands", "v", project.assets["v_anaconda"].path, category="pistols",
        derived={"from": "v_anaconda", "mode": "hands", "options": {}})
    tree.show_project(project)
    category = next(i for i in tree._walk() if i.data(0, ROLE_KIND) == "category")
    assert tree.retargetable(tree.assets_under(category)) == ["v_deagle"]


def test_budget_counts_a_map_from_anywhere(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from valve_qc_merger.studio.server_window import ServerWindow
    bsp = write_bsp(tmp_path / "zm_far.bsp", ENTITIES, 200)
    project = Project.create(tmp_path / "pack")
    window = ServerWindow(project)
    try:
        assert window.verdict_text().startswith("Without a map, after merge: fits")
        window.add_map(bsp)
        assert window.map_box.currentText() == "zm_far"
        assert window.bars["models"].numbers.text().startswith("371 / 512")  # 200+2+169
        assert window.verdict_text() == "With zm_far, after merge: fits — 141 model slot(s) free"
    finally:
        window.close()
