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
        win.derive_assets(win.explorer.selected_assets())
        dialog = win._derive_dialog
        assert isinstance(dialog, derive_dialog.DeriveDialog) and not dialog.isModal()
        dialog.mode_group.button(1).setChecked(True)
        dialog.apply_button.click()  # Apply: runs, the dialog stays
        assert win.jobs.wait(120_000)
        assert dialog.isVisible() and dialog.run_button.isEnabled()
        assert not dialog.name_edit.isEnabled()  # later Applies re-run this asset
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


def test_grip_tuning_round_trip(tmp_path: Path) -> None:
    from valve_qc_merger.handswap import tuning
    path = tmp_path / "grip_tuning.json"
    path.write_text('{"_comment": "keep me", "v_deagle": {"grip_offset": '
                    '{"left": [0, 0, -0.6]}}}')
    tuning.save_entry(str(path), "v_x", grip_offset={"right": [0.1, 0, 0],
                                                     "left": [0, 0, 0]},
                      weapon_offset=[0, 0.5, 0])
    assert tuning.load_entry(str(path), "v_x") == {
        "grip_offset": {"right": [0.1, 0.0, 0.0]}, "weapon_offset": [0.0, 0.5, 0.0]}
    assert tuning.load_entry(str(path), "v_deagle")["grip_offset"]["left"] == [0, 0, -0.6]
    assert "keep me" in path.read_text()
    tuning.save_entry(str(path), "v_x", grip_offset={}, weapon_offset=[0, 0, 0])
    assert "v_x" not in path.read_text()  # all-zero offsets drop the weapon


def test_retarget_reads_weapon_offset_from_tuning(tmp_path: Path) -> None:
    import shutil

    from valve_qc_merger.handswap import asset as assetmod
    from valve_qc_merger.handswap import tuning
    from valve_qc_merger.services.retarget import RetargetOptions, run_retarget
    asset = tmp_path / "hands" / Path(assetmod.DEFAULT_ASSET).name
    asset.parent.mkdir()
    shutil.copy(assetmod.DEFAULT_ASSET, asset)
    weapon = tmp_path / "v_anaconda"
    shutil.copytree(_ANACONDA, weapon)

    def run(name: str) -> dict[str, np.ndarray]:
        out = tmp_path / name
        assert run_retarget(RetargetOptions(weapon_dir=weapon, out=out, asset=asset),
                            CollectingReporter()).ok
        return _grip_world(out)

    plain = run("plain")
    tuning.save_entry(tuning.tuning_path(str(asset)), "v_anaconda",
                      weapon_offset=[0.0, 0.0, 1.0])
    tuned = run("tuned")
    assert np.allclose(tuned["Hand.L"] - plain["Hand.L"], [0.0, 0.0, -1.0], atol=1e-6)


def test_dialog_prefills_and_saves_tuning(app, tmp_path: Path, monkeypatch) -> None:
    from valve_qc_merger.handswap import tuning
    from valve_qc_merger.studio import derive_dialog
    path = tmp_path / "grip_tuning.json"
    monkeypatch.setattr(derive_dialog, "grip_tuning_file", lambda asset=None: path)
    tuning.save_entry(str(path), "v_x", grip_offset={"left": [0, 0, -0.4]})
    dialog = derive_dialog.DeriveDialog(["v_x"])
    assert dialog.grip_offsets["left"].value() == [0.0, 0.0, -0.4]
    assert "prefilled" in dialog.tuning_label.text()
    dialog.weapon_offset.set_value([0.0, 0.25, 0.0])
    dialog.save_tuning()
    assert tuning.load_entry(str(path), "v_x") == {
        "grip_offset": {"left": [0.0, 0.0, -0.4]}, "weapon_offset": [0.0, 0.25, 0.0]}
    # editing a derived asset shows ITS stored options, not the table
    stored = derive_dialog.DeriveDialog(["v_x"], options={}, name="v_x_hands")
    assert stored.weapon_offset.value() == [0.0, 0.0, 0.0]


def test_before_after_keeps_camera_pose_and_frame(app, tmp_path: Path) -> None:
    from PySide6.QtCore import QSettings

    from valve_qc_merger.studio.main_window import MainWindow
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_ANACONDA)
    project.derive_asset("v_anaconda", "canon")
    win = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    try:
        win.set_project(project)
        panel = win.viewport
        assert win.explorer.select("asset", "v_anaconda")
        assert not panel.compare_button.isVisibleTo(panel)  # not derived: no flip
        assert win.explorer.select("asset", "v_anaconda_canon")
        assert panel.compare_button.isVisibleTo(panel)
        assert "v_anaconda" in panel.compare_button.text()
        names = [panel.sequence_box.itemText(i) for i in range(panel.sequence_box.count())]
        reload = next(i for i, n in enumerate(names) if "reload" in n)
        panel.sequence_box.setCurrentIndex(reload)
        panel.slider.setValue(5)
        panel.viewport.state.camera.distance = 12.5
        derived_scene = panel.viewport.scene

        panel.compare_button.setChecked(True)  # before
        assert panel.viewport.scene is not derived_scene
        assert panel.sequence_box.currentIndex() == reload
        assert panel.viewport.state.frame == 5.0
        assert panel.viewport.state.camera.distance == 12.5
        panel.compare_button.setChecked(False)  # after
        assert panel.viewport.scene is derived_scene

        win._select_asset("v_anaconda_canon")  # e.g. after a re-run: view kept
        assert panel.viewport.state.camera.distance == 12.5
        assert panel.sequence_box.currentIndex() == reload
    finally:
        win.close()
