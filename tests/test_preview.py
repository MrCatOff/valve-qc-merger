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


def _rifle(angle_deg: float) -> np.ndarray:
    """Triangles of a side-on 'rifle' in the SMD frame (muzzle at -Y, up +Z):
    a long thin barrel in front, a tall magazine and stock behind, tilted."""
    def box(y0, y1, z0, z1):
        corners = np.array([[x, y, z] for x in (-0.5, 0.5) for y in (y0, y1)
                            for z in (z0, z1)])
        faces = [(0, 1, 3), (0, 3, 2), (4, 5, 7), (4, 7, 6), (0, 1, 5), (0, 5, 4),
                 (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 3, 7), (1, 7, 5)]
        return corners[np.array(faces)]
    parts = [box(-30, 0, 0, 1.5),      # barrel, muzzle at y = -30
             box(0, 12, -2, 3),        # receiver
             box(2, 5, -9, -2),        # magazine hanging below
             box(12, 22, -4, 3)]       # stock
    tris = np.concatenate(parts)
    a = np.radians(angle_deg)  # pitch the muzzle up: rotate in the Y-Z plane
    rot = np.array([[1, 0, 0], [0, np.cos(a), np.sin(a)], [0, -np.sin(a), np.cos(a)]])
    return tris @ rot.T, rot @ np.array([0.0, -1.0, 0.0])  # + the barrel's axis


@pytest.mark.parametrize("pitch", [-15.0, 0.0, 12.0, 20.0])
def test_side_view_levels_the_barrel(pitch: float) -> None:
    from valve_qc_merger.preview.render import surface_samples
    tris, direction = _rifle(pitch)
    right, up, _view = side_view(tris.reshape(-1, 3), tris)
    level = np.degrees(np.arctan2(direction @ up, direction @ right))
    assert abs(level) < 1.5  # the barrel lies flat whatever the idle's pitch
    assert direction @ right > 0  # muzzle to the right
    assert len(surface_samples(tris[:1])) == 45  # a 9 x 9 barycentric triangle
