"""Weapon previews: v_ models drawn without the hands, the catalog."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from valve_qc_merger.preview.catalog import build_catalog, preview_assets, weapon_name
from valve_qc_merger.preview.render import (
    Options,
    decode_png,
    encode_png,
    render_model,
    side_view,
    weapon_triangles,
)
from valve_qc_merger.project import Project
from valve_qc_merger.studio.scene import build_scene

_EXAMPLES = Path(__file__).parent / "examples"
_SMALL = Options(width=128, height=64, supersample=2)


def test_png_round_trip() -> None:
    rgba = np.random.default_rng(1).integers(0, 255, (5, 7, 4), dtype=np.uint8)
    data = encode_png(rgba)
    assert data.startswith(b"\x89PNG") and np.array_equal(decode_png(data), rgba)


def test_hands_leave_by_bodygroup() -> None:
    scene = build_scene(_EXAMPLES / "v_anaconda")
    parts, how = weapon_triangles(scene)
    assert how == "bodygroup" and {b.stem for b, _t in parts} == {"ref_Anaconda"}
    every, kept = weapon_triangles(scene, keep_hands=True)
    assert kept == "kept" and len(every) > len(parts)


def test_side_view_shows_the_right_side() -> None:
    # a flat "gun": long along -Y (the view model's forward), tall along Z
    rng = np.random.default_rng(0)
    points = rng.uniform([-0.5, -20, -3], [0.5, 0, 3], (500, 3))
    right, up, view = side_view(points)
    assert right @ [0, -1, 0] > 0.95 and up @ [0, 0, 1] > 0.95  # muzzle right, up up
    assert abs(view @ [1, 0, 0]) > 0.95


def test_render_preview() -> None:
    preview = render_model(_EXAMPLES / "v_anaconda", _SMALL)
    assert preview.rgba.shape == (64, 128, 4) and preview.hands == "bodygroup"
    alpha = preview.rgba[..., 3]
    assert alpha[0, 0] == 0 and (alpha > 0).mean() > 0.15  # transparent around the gun
    columns = np.flatnonzero(alpha.max(axis=0))
    assert columns[0] < 20 and columns[-1] > 108  # framed: fills the width
    # the speed loader the idle parks off the gun is left out
    assert any("left out" in w for w in preview.warnings)
    mirrored = render_model(_EXAMPLES / "v_anaconda", Options(128, 64, 2, barrel_left=True))
    assert np.array_equal(mirrored.rgba, preview.rgba[:, ::-1])


def test_catalog(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_EXAMPLES / "v_anaconda")
    project.import_decompiled(_EXAMPLES / "pair_deagle" / "v_deagle")
    project.assets["v_deagle"].notes = "Desert Eagle <50 AE>"
    project.assets["v_deagle"].category = "pistols"
    assets = preview_assets(project)
    assert [a.name for a in assets] == ["v_anaconda", "v_deagle"]
    assert [a.name for a in preview_assets(project, "pistols")] == ["v_deagle"]
    catalog = build_catalog(project, assets, tmp_path / "out", _SMALL)
    assert not catalog.failed and weapon_name("v_deagle") == "deagle"
    assert (tmp_path / "out" / "images" / "deagle.png").is_file()
    page = (tmp_path / "out" / "index.html").read_text(encoding="utf-8")
    assert "images/anaconda.png" in page and "Desert Eagle &lt;50 AE&gt;" in page
    assert "<h2>pistols</h2>" in page
    lines = (tmp_path / "out" / "weapons.txt").read_text(encoding="utf-8").splitlines()
    assert lines[2] == "deagle|pistols|images/deagle.png|Desert Eagle <50 AE>"


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_previews_from_the_studio(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QImage

    from valve_qc_merger.studio import dialogs
    from valve_qc_merger.studio.main_window import MainWindow
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_EXAMPLES / "v_anaconda")
    window = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    window._open_path = lambda _path: None
    try:
        window.set_project(project)
        original = dialogs.PreviewDialog.exec

        def accept(dialog) -> int:  # noqa: ANN001
            dialog.size_box.setCurrentIndex(0)
            dialog.columns_spin.setValue(2)
            return dialogs.PreviewDialog.DialogCode.Accepted

        dialogs.PreviewDialog.exec = accept
        try:
            window.make_previews()
        finally:
            dialogs.PreviewDialog.exec = original
        assert window.jobs.wait(120_000)
        QtWidgets.QApplication.processEvents()
        out = project.root / "previews"
        assert (out / "images" / "anaconda.png").is_file()
        sheet = QImage(str(out / "sheet.png"))
        assert sheet.width() == 256 + 2 * 12 + 12  # one card: the sheet is not wider
        assert "1 preview(s)" in window.log.toPlainText()
    finally:
        window.close()
