"""The retarget check: a swap-hands result against its original, every frame."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from valve_qc_merger.handswap.audit import _spikes, audit_retarget, on_screen
from valve_qc_merger.project import Project

_ANACONDA = Path("tests/examples/v_anaconda")


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Project:
    project = Project.create(tmp_path_factory.mktemp("audit") / "pack")
    project.import_decompiled(_ANACONDA)
    result, _asset = project.derive_asset("v_anaconda", "hands")
    assert result.ok
    return project


def test_a_retarget_is_checked_as_it_is_made(project: Project) -> None:
    audit = project.assets["v_anaconda_hands"].derived["audit"]
    assert audit["weapon"] < 0.05 and audit["problems"] == []
    assert audit["frames"] > 0 and audit["fit"] < 0.5


def test_the_check_measures_every_sequence(project: Project) -> None:
    audit = audit_retarget(project.asset_dir("v_anaconda"),
                           project.asset_dir("v_anaconda_hands"))
    assert {s.name for s in audit.sequences} >= {"idle1", "shoot1"}
    assert audit.weapon < 0.05 and not audit.problems


def test_project_audit_keeps_the_verdict(project: Project) -> None:
    results = project.audit_swaps()
    assert set(results) == {"v_anaconda_hands"}
    assert Project.open(project.root).assets["v_anaconda_hands"].derived["audit"] \
        == results["v_anaconda_hands"]


def test_view_and_spikes() -> None:
    import numpy as np
    seen = on_screen(np.array([[0.0, -20.0, 0.0], [0.0, 5.0, 0.0], [60.0, -10.0, 0.0]]))
    assert seen.tolist() == [True, False, False]  # ahead / behind / far off to the side
    # a frame whose speed spikes over its neighbours while the original's
    # wrist does not is a jump; one where the wrist spikes too is not
    assert _spikes([1.0, 1.0, 12.0, 1.0, 1.0], [1.0] * 5) == [2]
    assert _spikes([1.0, 1.0, 12.0, 1.0, 1.0], [1.0, 1.0, 9.0, 1.0, 1.0]) == []


def test_cli_project_audit(project: Project, capsys: pytest.CaptureFixture[str]) -> None:
    from valve_qc_merger.cli import main
    assert main(["project", "audit", str(project.root)]) == 0
    assert "1 swap-hands result(s) checked: 0 with problems" in capsys.readouterr().out


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_compare_dialog_shows_both(project: Project) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from valve_qc_merger.studio.compare_dialog import CompareDialog
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = CompareDialog(project.asset_dir("v_anaconda"),
                           project.asset_dir("v_anaconda_hands"), "v_anaconda_hands",
                           project.assets["v_anaconda_hands"].derived["audit"])
    assert dialog.sequence_box.count() >= 2
    pixmap = dialog.picture.pixmap()
    assert pixmap is not None and pixmap.height() > 300  # two rows of frames
    dialog.close()
