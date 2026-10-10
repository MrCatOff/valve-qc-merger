"""Studio M4: build reports (Qt-free) and the build panel (offscreen)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_merge_zhands import _write_model
from valve_qc_merger.project import Build, Project
from valve_qc_merger.services.base import CollectingReporter
from valve_qc_merger.studio.build_report import (
    LIMITS,
    manifest_rows,
    part_stats,
    record_part_stats,
)
from valve_qc_merger.studio.scene import build_scene

_WORLD = Path("tests/examples/world")


@pytest.fixture()
def zombie_project(tmp_path: Path) -> Project:
    src = tmp_path / "src"
    _write_model(src, "v_alpha_knife", grenade=False, hand_root="Bone01")
    _write_model(src, "v_alpha_grenade", grenade=True, hand_root="Bone01")
    _write_model(src, "v_alpha_knife_invisible", grenade=False, hand_root="Bone01",
                 render="$texrendermode hand.bmp additive")
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(src)
    project.add_build(Build("zh", "merge-zhands"))
    return project


def test_part_stats_and_manifest(zombie_project: Project) -> None:
    result = zombie_project.run_build("zh", CollectingReporter())
    assert result.ok
    record = zombie_project.build_dir("zh") / "last_run.json"
    (stats,) = record_part_stats(record, zombie_project.root)
    # grenade {blank, grenade} + hands {blank, ...}: every group leads with a blank
    assert (stats.bodyparts, stats.submodels, stats.sequences) == (2, 5, 2)
    assert stats.bones == 3 and not stats.over() and stats.seq_bytes > 0
    assert json.loads(record.read_text())["parts"][0]["name"] == "v_zhands"
    header, rows = manifest_rows(zombie_project.build_dir("zh") / "output")
    assert header[:3] == ["asset", "pev_body", "hands"]
    by_asset = {row[0]: row for row in rows}
    assert by_asset["v_alpha_grenade"][1] == "3"  # the hands group leads with a blank


def test_pev_body_decodes_to_bodygroup_entries(zombie_project: Project) -> None:
    zombie_project.run_build("zh", CollectingReporter())
    scene = build_scene(zombie_project.build_dir("zh") / "output")
    assert list(scene.groups) == ["grenade", "hands"]
    assert scene.decode_body(0) == {"grenade": 0, "hands": 0}
    assert scene.decode_body(1) == {"grenade": 1, "hands": 0}  # grenade = low bit
    assert scene.decode_body(2) == {"grenade": 0, "hands": 1}


def test_part_stats_flags_over_limits(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_WORLD)
    project.add_build(Build("w", "merge-w"))
    project.run_build("w", CollectingReporter())
    qc = project.root / json.loads(
        (project.build_dir("w") / "last_run.json").read_text())["outputs"][0]
    stats = part_stats(qc, project.root)
    assert stats.bones <= LIMITS["bones"] and stats.submodels >= len(list(_WORLD.iterdir()))
    stats.bones = 200
    assert stats.over() == ["bones"]


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
    win.jobs.cancel()
    win.jobs.wait(10_000)
    win.close()


def test_build_panel_edit_run_and_preview(window, zombie_project: Project) -> None:
    window.set_project(zombie_project)
    assert window.explorer.select("build", "zh")
    panel = window.build_panel
    assert window.right.currentWidget() is panel
    # edit an option through the generated form and save it
    form = panel.options_form
    form._widgets["name"].setText("v_zombies")
    assert form._widgets["manifest_format"].currentText() == "ini"  # choices -> combo
    assert panel.save()
    assert zombie_project.builds["zh"].options == {"name": "v_zombies"}
    reopened = Project.open(zombie_project.root)
    assert reopened.builds["zh"].options == {"name": "v_zombies"}

    window.run_build("zh")
    assert window.jobs.wait(60_000)
    assert panel.currentIndex() == 1  # Results shown after the run
    assert panel.gates.rowCount() >= 2 and panel.budgets.rowCount() == 1
    assert panel.outputs.count() == 1
    assert panel.manifest.rowCount() == 3

    rows = {panel.manifest.item(r, 0).text(): r for r in range(panel.manifest.rowCount())}
    panel._preview_row(rows["v_alpha_grenade"])
    assert window.viewport.viewport.state.bodygroups["grenade"] == 1


def test_pick_assets_requires_one(window, zombie_project: Project, monkeypatch) -> None:
    window.set_project(zombie_project)
    window.explorer.select("build", "zh")
    panel = window.build_panel
    panel.pick_radio.setChecked(True)
    warnings = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda *a, **k: warnings.append(a[2]))
    assert not panel.save()
    assert warnings and "at least one" in warnings[0]


def test_compile_before_run_offers_to_run_first(window, zombie_project: Project,
                                                monkeypatch) -> None:
    zombie_project.settings.studiomdl = str(Path("does-not-matter"))
    window.set_project(zombie_project)
    window.explorer.select("build", "zh")
    asked = []

    def answer(*args, **kwargs):
        asked.append(args[2])
        return QtWidgets.QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", answer)
    window.compile_build("zh")
    assert asked and "has not been run" in asked[0]
    assert window.jobs.wait(60_000)
    record = json.loads((zombie_project.build_dir("zh") / "last_run.json").read_text())
    assert record["outputs"]  # the build ran (the fake studiomdl then fails to start)
    assert "Build + compile zh" in window.log.toPlainText()
