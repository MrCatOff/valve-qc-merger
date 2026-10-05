"""Asset status: our hands / own hands, stale derived assets, build problems."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from valve_qc_merger.project import Build, Project
from valve_qc_merger.project.status import project_status

_ANACONDA = Path("tests/examples/v_anaconda")


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Project:
    project = Project.create(tmp_path_factory.mktemp("status") / "pack")
    project.import_decompiled(_ANACONDA)
    result, _asset = project.derive_asset("v_anaconda", "hands")
    assert result.ok
    project.add_build(Build("view", "merge-v", retarget=True))
    return project


def test_hand_check_ignores_bone_names(project: Project, tmp_path: Path) -> None:
    """Renamed bones still count as our hands; our bone NAMES on other
    geometry do not (Hand.L is plain Blender convention)."""
    from valve_qc_merger.merge_view.handcheck import smd_is_reference
    from valve_qc_merger.resources import resource_path
    from valve_qc_merger.retarget.config import DEFAULT_SHARED_HANDS_REFERENCE
    reference = resource_path(Path(DEFAULT_SHARED_HANDS_REFERENCE))
    ours = project.asset_dir("v_anaconda_hands") / "hands.smd"
    text = ours.read_text(encoding="latin-1")
    renamed = tmp_path / "renamed.smd"
    renamed.write_text(text.replace('"Hand.L"', '"L_Wrist"').replace('"Hand.R"', '"R_Wrist"'),
                       encoding="latin-1")
    assert smd_is_reference(ours, reference) and smd_is_reference(renamed, reference)
    raw = project.asset_dir("v_anaconda") / "grafted_male.smd"  # same triangle count
    fake = tmp_path / "fake.smd"
    fake.write_text(raw.read_text(encoding="latin-1").replace(
        '"Bip01_L_Hand"', '"Hand.L"').replace('"Bip01_R_Hand"', '"Hand.R"'),
        encoding="latin-1")
    assert not smd_is_reference(raw, reference) and not smd_is_reference(fake, reference)


def test_hands_builds_and_staleness(project: Project) -> None:
    status = project_status(project)
    raw, hands = status["v_anaconda"], status["v_anaconda_hands"]
    assert (raw.hands, hands.hands) == ("own", "ours")
    assert raw.level == "own" and hands.level == "ours"
    # the source is the same weapon as its swap-hands result: left out of view
    assert raw.builds == [] and hands.builds == ["view"]
    assert raw.replaced == ["view: by v_anaconda_hands"]
    assert not hands.stale
    # editing the source after the retarget makes the derived asset stale
    qc = next(project.asset_dir("v_anaconda").glob("*.qc"))
    later = time.time() + 10
    os.utime(qc, (later, later))
    status = project_status(project)
    assert status["v_anaconda_hands"].stale
    assert status["v_anaconda_hands"].level == "stale"
    assert any("Re-run" in line for line in status["v_anaconda_hands"].lines())


def test_last_run_failures_become_problems(project: Project) -> None:
    record = project.build_dir("view") / "last_run.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"failures": [
        "model 'v_anaconda': wears other hands than the 1 other model(s)",
        "retarget v_anaconda_hands failed (boom); left out of the merge",
        "model 'not_an_asset': whatever"]}))
    status = project_status(project)
    assert status["v_anaconda"].level == "problem"
    assert status["v_anaconda"].problems == [
        "view: model 'v_anaconda': wears other hands than the 1 other model(s)"]
    assert status["v_anaconda_hands"].problems[0].startswith("view: retarget")
    record.unlink()


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_explorer_badges_and_inspector(project: Project, tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings

    from valve_qc_merger.studio.main_window import MainWindow
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    try:
        win.set_project(project)
        assert win.explorer.select("asset", "v_anaconda")
        item = win.explorer.currentItem()
        assert not item.icon(0).isNull()
        assert "own hands" in item.toolTip(0)
        assert "left out of view: by v_anaconda_hands" in item.toolTip(0)
        assert "own hands" in win.inspector.status_label.text()
    finally:
        win.close()


def test_plan_tab(project: Project, tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings

    from valve_qc_merger.studio.main_window import MainWindow
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    try:
        win.set_project(project)
        win.plan_build("view")
        assert win.jobs.wait(120_000)
        panel = win.build_panel
        assert panel.currentIndex() == panel.plan_tab
        # v_anaconda and its swap-hands result are one weapon: only the
        # swap-hands asset (the tuned grip) is planned
        assert panel.plan_table.rowCount() == 1
        assert "1 part(s)" in panel.plan_summary.text()
        assert "the same weapon twice" not in panel.plan_rejected.text()
        panel._plan_row_activated(0, 0)
        assert win.explorer.current_asset() == panel.plan_table.item(0, 2).text()
        assert panel.plan_table.item(0, 2).text() == "v_anaconda_hands"
        assert not (project.build_dir("view") / "output").exists()
    finally:
        win.close()


def test_retarget_build_takes_a_weapon_once(project: Project) -> None:
    """With "on our hands first" a weapon and its swap-hands result are not
    merged twice: the swap-hands asset wins (explicit lists too)."""
    view = project.builds["view"]
    assert [a.name for a in project.build_assets(view)] == ["v_anaconda_hands"]
    assert project.superseded_assets(view, list(project.assets.values())) == {
        "v_anaconda": "v_anaconda_hands"}
    explicit = Build("explicit", "merge-v", assets=["v_anaconda", "v_anaconda_hands"],
                     retarget=True)
    assert [a.name for a in project.build_assets(explicit)] == ["v_anaconda_hands"]
    own = Build("own", "merge-v")  # own hands: nothing is swapped, both stay
    assert len(project.build_assets(own)) == 2


def test_long_plan_lists_do_not_stretch_the_panel() -> None:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from valve_qc_merger.studio.build_panel import BuildPanel
    panel = BuildPanel()
    before = panel.minimumSizeHint().height()
    long = "\n".join(f"✗ model 'v_{i}': multi-part weapon rejected" for i in range(80))
    panel.plan_rejected.setText(long)
    panel.failures.setText(long)
    assert panel.plan_rejected.text() == long
    assert panel.minimumSizeHint().height() <= before + 320  # two capped lists at most
