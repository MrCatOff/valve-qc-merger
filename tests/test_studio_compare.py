"""Hitboxes in the viewport and comparing any two assets."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from valve_qc_merger.project import Project
from valve_qc_merger.studio.scene import build_scene, parse_hitboxes

_EXAMPLES = Path(__file__).parent / "examples"


def test_hitboxes_parse_and_pose() -> None:
    from valve_qc_merger.studio.renderer import HITGROUP_COLORS, hitbox_lines
    boxes = parse_hitboxes('$hbox 1 "Bip01 Head" -1 -2 -3 4 5 6\n'
                           '$hbox 2 "missing bone" 0 0 0 1 1 1\n', ["Bip01", "Bip01 Head"])
    assert len(boxes) == 1 and boxes[0][0] == 1 and boxes[0][1] == 1
    assert boxes[0][2].tolist() == [-1, -2, -3] and boxes[0][3].tolist() == [4, 5, 6]
    rot = np.tile(np.eye(3), (2, 1, 1))
    trans = np.array([[0.0, 0, 0], [10.0, 0, 0]])
    lines = hitbox_lines(boxes, rot, trans)
    assert lines.shape == (24, 6)  # 12 edges
    assert lines[:, 0].min() == 9.0 and lines[:, 0].max() == 14.0  # moved by the bone
    assert tuple(lines[0, 3:]) == pytest.approx(HITGROUP_COLORS[1])
    scene = build_scene(_EXAMPLES / "v_anaconda")
    assert len(scene.hitboxes) == 2


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


def test_hitbox_toggle(window) -> None:
    panel = window.viewport
    assert not panel.viewport.state.show_hitboxes
    panel.hitbox_box.setChecked(True)
    assert panel.viewport.state.show_hitboxes


def test_compare_with_any_asset(window, tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_EXAMPLES / "v_anaconda")
    project.import_decompiled(_EXAMPLES / "pair_deagle" / "v_deagle")
    window.set_project(project)
    window.explorer.select("asset", "v_anaconda")
    panel = window.viewport
    assert not panel.compare_button.isEnabled()  # not derived: nothing to compare yet
    window.compare_assets("v_anaconda", "v_deagle")
    assert panel.compare_button.isEnabled() and panel.compare_button.text() == "Compare"
    panel.compare_button.setChecked(True)
    assert panel.viewport.scene.name == "v_deagle"
    assert panel.compare_badge.text() == "COMPARE  ·  v_deagle"
    panel.compare_button.setChecked(False)
    assert panel.viewport.scene.name == "v_anaconda"
    window.explorer.select("asset", "v_deagle")  # another asset: the pair is gone
    assert not panel.compare_button.isEnabled()
