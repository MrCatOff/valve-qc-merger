"""Retarget in the studio: derived assets (hand swap / canonical bones),
the weapon offset, and the Retarget dialog."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.project import Project, ProjectError
from valve_qc_merger.services.base import CollectingReporter
from valve_qc_merger.services.canonicalize import CanonicalizeOptions, run_canonicalize

_ANACONDA = Path("tests/examples/v_anaconda")


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Project:
    project = Project.create(tmp_path_factory.mktemp("derive") / "pack")
    project.import_decompiled(_ANACONDA)
    return project


def _grip_world(directory: Path) -> dict[str, np.ndarray]:
    from valve_qc_merger.handswap import build as buildmod
    model = buildmod.load_weapon(str(directory), None, log=lambda *a: None)
    seq = model.qc.sequences[0]["smd"]
    return {k: m[:3, 3] for k, m in
            buildmod.anim_world_frames(model.anims[seq], model.skel)[0].items()}


def test_canonicalize_keeps_own_hands_and_pose(tmp_path: Path) -> None:
    out = tmp_path / "canon"
    result = run_canonicalize(CanonicalizeOptions(_ANACONDA, out), CollectingReporter())
    assert result.ok and result.data["canonical"]["max_pose_deviation"] < 1e-4
    model = load_model(out)
    assert model.bone_names[0] == "Bip01"
    assert "ValveBiped.Bip01_L_Hand" in model.bone_names
    assert set(model.meshes) == set(load_model(_ANACONDA).meshes)  # own hands kept
    assert not any(name.endswith("Nub") for name in model.bone_names)


def test_derive_swap_with_weapon_offset_then_rerun(project: Project) -> None:
    result, base = project.derive_asset("v_anaconda", "hands")
    assert result.ok and base is not None and base.name == "v_anaconda_hands"
    before = _grip_world(project.asset_dir(base.name))

    result, moved = project.rederive_asset(base.name, {"weapon_offset": [0.0, 0.0, 1.0]})
    assert result.ok and moved.derived["options"] == {"weapon_offset": [0.0, 0.0, 1.0]}
    after = _grip_world(project.asset_dir(base.name))
    # the weapon stays put, both hands move by the opposite vector
    assert np.allclose(after["Bone02"], before["Bone02"])
    for wrist in ("Hand.L", "Hand.R"):
        assert np.allclose(after[wrist] - before[wrist], [0.0, 0.0, -1.0], atol=1e-6)
    assert project.can_undo(base.name)  # the re-run snapshotted the old output

    reopened = Project.open(project.root)
    assert reopened.assets[base.name].derived == {
        "from": "v_anaconda", "mode": "hands",
        "options": {"weapon_offset": [0.0, 0.0, 1.0]}}


def test_derive_canon_and_guards(project: Project) -> None:
    result, asset = project.derive_asset("v_anaconda", "canon", name="v_ana_c")
    assert result.ok and asset.kind == "v" and asset.derived["mode"] == "canon"
    assert load_model(project.asset_dir("v_ana_c")).bone_names[0] == "Bip01"
    with pytest.raises(ProjectError):
        project.derive_asset("v_anaconda", "canon", name="v_anaconda")
    with pytest.raises(ProjectError):
        project.rederive_asset("v_anaconda")  # imported, not derived
    with pytest.raises(ProjectError):
        project.derive_asset("v_anaconda", "nope")
    assert not (project.root / ".derive").exists()


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture()
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_dialog_round_trips_offsets(app) -> None:
    from valve_qc_merger.studio.derive_dialog import DeriveDialog
    options = {"weapon_offset": [0.0, 0.5, 0.0], "grip_offset": ["left:0,0,-0.4"],
               "snug": False}
    dialog = DeriveDialog(["v_x"], mode="hands", options=options, name="v_x_hands")
    assert dialog.result_spec() == ("hands", "v_x_hands", options)
    dialog.grip_offsets["right"].set_value([0.1, 0.0, 0.0])
    dialog.weapon_offset.set_value([0.0, 0.0, 0.0])
    _mode, _name, changed = dialog.result_spec()
    assert "weapon_offset" not in changed
    assert changed["grip_offset"] == ["left:0,0,-0.4", "right:0.1,0,0"]


def test_dialog_names_follow_the_mode(app) -> None:
    from valve_qc_merger.studio.derive_dialog import DeriveDialog
    dialog = DeriveDialog(["v_x"])
    assert dialog.target_name() == "v_x_hands"
    dialog.mode_group.button(1).setChecked(True)
    assert dialog.result_spec() == ("canon", "v_x_canon", {})
    batch = DeriveDialog(["v_a", "v_b"])
    assert batch.target_name() is None and not batch.name_edit.isEnabled()


def test_window_retargets_selected_asset(app, tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtCore import QSettings

    from valve_qc_merger.studio import derive_dialog
    from valve_qc_merger.studio.main_window import MainWindow
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_ANACONDA)
    win = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    try:
        win.set_project(project)
        assert win.explorer.select("asset", "v_anaconda")
        monkeypatch.setattr(derive_dialog.DeriveDialog, "exec",
                            lambda self: self.mode_group.button(1).setChecked(True) or
                            derive_dialog.DeriveDialog.DialogCode.Accepted)
        win.derive_assets(win.explorer.selected_assets())
        assert win.jobs.wait(120_000)
        assert "v_anaconda_canon" in project.assets
        assert win.explorer.current_asset() == "v_anaconda_canon"
        assert "canon from asset v_anaconda" in win.inspector.source_label.text()
    finally:
        win.jobs.cancel()
        win.jobs.wait(10_000)
        win.close()


def test_options_form_keeps_comma_entries_whole(app) -> None:
    from valve_qc_merger.services.retarget import RetargetOptions
    from valve_qc_merger.studio.options_form import OptionsForm
    values = {"grip_offset": ["left:0,0,-0.4", "right:0.1,0,0"],
              "weapon_offset": [0.0, 0.5, 0.0]}
    assert OptionsForm(RetargetOptions, "retarget", values).values() == values
