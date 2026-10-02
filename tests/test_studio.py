"""Studio M2: model summary (Qt-free) and offscreen GUI smoke tests."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from valve_qc_merger.studio.model_info import read_model_info, texture_rgba

_MINI = Path("tests/examples/mdl/src")
_WORLD = Path("tests/examples/world")


def test_model_info_summarises_the_fixture() -> None:
    info = read_model_info(_MINI)
    assert [(s.group, s.stem, s.triangles) for s in info.submodels] == [
        ("body", "body", 2), ("gun", "blank", 0), ("gun", "gun", 1)]
    textures = {t.name: t for t in info.textures}
    assert (textures["glass.bmp"].width, textures["glass.bmp"].height) == (8, 8)
    assert textures["glass.bmp"].render_mode == "masked"
    assert textures["skin.bmp"].used_by == ["body"]
    assert [(s.name, s.fps, s.frames, s.loop) for s in info.sequences] == [
        ("idle", 15.0, 2, True), ("shoot", 30.0, 3, False)]
    assert [(b.name, b.parent) for b in info.bones] == [("root", -1), ("gun", 0)]
    assert info.attachments[0].bone == "gun" and info.attachments[0].offset == (1, 2, 3)


def test_texture_rgba_masks_index_255(tmp_path: Path) -> None:
    from valve_qc_merger.merge_view.bmp8 import Bmp8, write_bmp8
    path = tmp_path / "t.bmp"
    path.write_bytes(write_bmp8(Bmp8(4, 1, [(i, i, i) for i in range(256)],
                                     bytearray([0, 1, 255, 2]))))
    _w, _h, opaque = texture_rgba(path)
    _w, _h, masked = texture_rgba(path, masked=True)
    assert opaque[11] == 255 and masked[11] == 0 and masked[3] == 255


# --------------------------------------------------------------------------- #
# GUI (offscreen; skipped without PySide6)
# --------------------------------------------------------------------------- #
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    instance = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield instance


@pytest.fixture()
def window(app, tmp_path: Path):
    from PySide6.QtCore import QSettings

    from valve_qc_merger.studio.main_window import MainWindow
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings)
    yield win
    win.jobs.cancel()
    win.jobs.wait(10_000)
    win.close()


def _select(window, name: str) -> None:
    from valve_qc_merger.studio.widgets import ROLE_NAME

    def find(item):
        if item.data(0, ROLE_NAME) == name:
            return item
        for i in range(item.childCount()):
            hit = find(item.child(i))
            if hit is not None:
                return hit
        return None

    for i in range(window.explorer.topLevelItemCount()):
        hit = find(window.explorer.topLevelItem(i))
        if hit is not None:
            window.explorer.setCurrentItem(hit)
            return
    raise AssertionError(f"{name} not in the explorer")


def test_project_import_and_inspect(window, tmp_path: Path) -> None:
    from valve_qc_merger.project import Project

    window.set_project(Project.create(tmp_path / "pack", "Pack"))
    assert window.act_import_mdl.isEnabled()
    window._run_import("Import .mdl", [Path("tests/examples/mdl/mini.mdl")], mdl=True)
    assert not window.act_import_mdl.isEnabled()  # disabled while the job runs
    assert window.jobs.wait(60_000)
    window._run_import("Import decompiled", [_WORLD], mdl=False)
    assert window.jobs.wait(60_000)
    assert set(window.project.assets) == {"mini", *(p.name for p in _WORLD.iterdir())}
    assert "+ mini" in window.log.toPlainText()

    _select(window, "mini")
    assert window.inspector.name_label.text() == "mini"
    assert window.inspector.sequences.rowCount() == 2
    assert window.inspector.textures.rowCount() == 2
    assert window.inspector.bones.topLevelItem(0).text(0) == "root"

    window.set_kind("mini", "v")
    assert window.project.assets["mini"].kind == "v"
    reopened = Project.open(tmp_path / "pack")
    assert reopened.assets["mini"].kind == "v"


def test_recent_projects_and_reopen(window, tmp_path: Path) -> None:
    from valve_qc_merger.project import Project

    Project.create(tmp_path / "a", "A")
    assert window.open_project(tmp_path / "a")
    assert window.windowTitle().startswith("A")
    assert str(tmp_path / "a") in window._recent()
    window.close_project()
    assert window.project is None and not window.act_import_mdl.isEnabled()


def test_job_cancel(window) -> None:
    import time

    outcome = []
    window.jobs.done.connect(lambda title, ok, payload: outcome.append((ok, payload)))

    def work(reporter):
        for _ in range(500):
            reporter.check()
            time.sleep(0.01)
        return "finished"

    assert window.jobs.start("spin", work)
    assert not window.jobs.start("second", work)  # one job at a time
    window.jobs.cancel()
    assert window.jobs.wait(10_000)
    assert outcome == [(False, "cancelled")]


def test_broken_asset_does_not_break_the_window(window, tmp_path: Path) -> None:
    from valve_qc_merger.project import Project

    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_WORLD / "w_luger")
    shutil.rmtree(project.asset_dir("w_luger"))
    project.asset_dir("w_luger").mkdir(parents=True)
    (project.asset_dir("w_luger") / "w_luger.qc").write_text('$body studio "missing"\n')
    window.set_project(project)
    _select(window, "w_luger")
    assert "cannot read model" in window.log.toPlainText()
