"""The studio theme, icons and window chrome (toolbar, welcome, layout)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from valve_qc_merger.project import Build, Project  # noqa: E402

_ANACONDA = Path(__file__).parent / "examples" / "v_anaconda"


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app, tmp_path: Path):
    from PySide6.QtCore import QSettings

    from valve_qc_merger.studio.main_window import MainWindow
    win = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    yield win
    win.close()


def test_every_bundled_icon_renders(app) -> None:
    from valve_qc_merger.studio import theme
    from valve_qc_merger.studio.icons import ICON_DIR, icon
    names = sorted(p.stem for p in ICON_DIR.glob("*.svg"))
    assert "play" in names and (ICON_DIR / "LICENSE").is_file()
    for name in names:
        assert not icon(name).pixmap(16, 16).isNull(), name
    sheet = theme.style_sheet()
    assert theme.TOKENS["accent"] in sheet and "{" in sheet


def test_toolbar_icons_and_actions_follow_the_selection(window, tmp_path: Path) -> None:
    from valve_qc_merger.studio.main_window import MainWindow
    for attr, _icon, _label in MainWindow.ACTION_ICONS:
        assert not getattr(window, attr).icon().isNull(), attr
    assert window.center.currentWidget() is window.welcome
    assert not window.act_run_build.isEnabled() and not window.act_derive.isEnabled()
    assert window.counts_label.text() == "No project"

    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_ANACONDA)
    project.add_build(Build("view", "merge-v"))
    window.set_project(project)
    assert window.center.currentWidget() is window.viewport
    assert window.counts_label.text() == "pack  ·  1 asset  ·  1 build"
    window.explorer.select("asset", "v_anaconda")
    assert window.act_derive.isEnabled() and not window.act_run_build.isEnabled()
    window.explorer.select("build", "view")
    assert window.act_run_build.isEnabled() and not window.act_derive.isEnabled()
    window.close_project()
    assert window.center.currentWidget() is window.welcome


def test_welcome_lists_recent_projects(window, tmp_path: Path) -> None:
    project = Project.create(tmp_path / "recent_pack")
    window.set_project(project)
    window.close_project()
    links = [w for w in window.welcome.findChildren(QtWidgets.QPushButton)
             if w.property("role") == "link"]
    assert [b.text() for b in links] == ["recent_pack"]
    opened: list[str] = []
    window.welcome.recent_requested.connect(opened.append)
    links[0].click()
    assert opened == [str(tmp_path / "recent_pack")]


def test_reset_layout_brings_hidden_docks_back(window) -> None:
    window.show()
    dock = window.docks[0]
    dock.close()
    window.toolbar.hide()
    assert not dock.isVisible()
    window.reset_layout()
    assert dock.isVisible() and window.toolbar.isVisible()


# -- forms (phase 2) ---------------------------------------------------------
def test_options_form_labels_invert_and_keep_hidden(app) -> None:
    from valve_qc_merger.services.merge_view import MergeViewOptions
    from valve_qc_merger.services.retarget import RetargetOptions
    from valve_qc_merger.studio.options_form import OptionsForm
    form = OptionsForm(MergeViewOptions, "merge-v", {"no_verify": True, "dry_run": True})
    labels = {w.text() for w in form.findChildren(QtWidgets.QLabel)}
    assert {"Output name", "Verify the result", "Pool bones"} <= labels
    assert "dry_run" not in form._widgets and "plan_only" not in form._widgets
    verify = form._widgets["no_verify"]
    assert not verify.isChecked()  # no_verify=True is shown as "Verify" unchecked
    # the advanced section opens because one of its options is set
    assert form.advanced_toggle is not None and form.advanced_toggle.isChecked()
    assert form.values() == {"no_verify": True, "dry_run": True}  # hidden value kept
    verify.setChecked(True)
    assert form.values() == {"dry_run": True}

    plain = OptionsForm(RetargetOptions, "retarget", {})
    assert not plain.advanced_toggle.isChecked() and plain.advanced_box.isHidden()
    assert "category" not in plain._widgets and "studiomdl" not in plain._widgets
    max_fit = plain._widgets["snug_max_deg"]
    assert max_fit.placeholderText().startswith("weapon's grip tuning")
    from PySide6.QtGui import QValidator
    check = max_fit.validator().validate  # letters and decimal commas are refused
    assert check("4.5", 0)[0] == QValidator.State.Acceptable
    assert check("4x", 0)[0] == QValidator.State.Invalid
    assert check("4,5", 0)[0] == QValidator.State.Invalid
    offset = plain._widgets["weapon_offset"]
    assert offset.validator().validate("0, 0.5, -1", 0)[0] == QValidator.State.Acceptable


def test_build_settings_footer_and_shared_hands(window, tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_ANACONDA)
    project.add_build(Build("view", "merge-v", retarget=True))
    window.set_project(project)
    window.explorer.select("build", "view")
    panel = window.build_panel
    assert not panel.settings_footer.isHidden() and not panel.plan_button.isHidden()
    shared = panel.options_form._widgets["shared_hands"]
    assert not shared.isEnabled()  # "on our hands first" implies shared hands
    panel.retarget_check.setChecked(False)
    assert shared.isEnabled()
    window.close_project()
    panel.show_build(None, "")
    assert panel.settings_footer.isHidden()


def test_new_build_dialog_describes_the_kind(window, tmp_path: Path) -> None:
    from valve_qc_merger.studio.build_panel import KIND_DESCRIPTIONS, NewBuildDialog
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_ANACONDA)
    dialog = NewBuildDialog(project, window)
    assert dialog.kind_box.currentData() == "merge-v"
    assert dialog.kind_box.currentText().startswith("View models")
    assert dialog.kind_hint.text() == KIND_DESCRIPTIONS["merge-v"]


# -- viewport (phase 3) ------------------------------------------------------
def test_floor_grid_sits_under_the_model() -> None:
    import numpy as np

    from valve_qc_merger.studio.renderer import GRID_X, GRID_Y, _nice_step, grid_lines
    assert [_nice_step(v) for v in (0.7, 1.0, 1.3, 3.0, 7.0, 42.0)] == [1, 1, 2, 5, 10, 50]
    lines = grid_lines(np.array([-10.0, -5.0, -3.0]), np.array([10.0, 5.0, 7.0]))
    assert lines.shape[1] == 6 and len(lines) % 2 == 0
    assert np.allclose(lines[:, 2], -3.0)  # on the model's lowest point
    for axis_colour in (GRID_X, GRID_Y):  # both axes through the origin are drawn
        assert np.isclose(lines[:, 3:], axis_colour, atol=1e-6).all(axis=1).any()


def test_viewport_toggles_timeline_and_compare_badge(app) -> None:
    from valve_qc_merger.studio.viewport import ViewportPanel
    panel = ViewportPanel()
    state = panel.viewport.state
    assert state.show_grid and panel.grid_box.isChecked()
    panel.grid_box.setChecked(False)
    panel.wire_box.setChecked(True)
    assert not state.show_grid and state.wireframe
    panel.set_scene(build_scene_for_test())
    frames = panel.viewport.scene.sequences[0].frames
    panel.step(-1)  # wraps to the last frame
    assert panel.slider.value() == frames - 1 and not panel.play_button.isChecked()
    panel.step(1)
    assert panel.slider.value() == 0
    panel.set_compare("v_src")
    panel.compare_button.setChecked(True)
    assert not panel.compare_badge.isHidden()
    assert panel.compare_badge.text() == "BEFORE  ·  v_src"
    panel.set_compare(None)
    assert panel.compare_badge.isHidden() and not panel.compare_button.isEnabled()


def build_scene_for_test():
    from valve_qc_merger.studio.scene import build_scene
    return build_scene(_ANACONDA)


# -- inspector (phase 4) -----------------------------------------------------
def test_inspector_header_tiles_and_tabs(window, tmp_path: Path) -> None:
    from valve_qc_merger.services.base import CollectingReporter
    inspector = window.inspector
    assert inspector.pages.currentWidget() is inspector.empty_page
    assert [inspector.tabs.tabText(i) for i in range(inspector.tabs.count())] == [
        "Overview", "Geometry", "Animation", "Bones", "QC"]
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_ANACONDA)
    project.derive_asset("v_anaconda", "hands", reporter=CollectingReporter())
    window.set_project(project)
    window.explorer.select("asset", "v_anaconda")
    assert inspector.pages.currentWidget() is not inspector.empty_page
    assert inspector.name_label.text() == "v_anaconda"
    assert inspector.kind_badge.text() == "View models (v_)"
    assert inspector.path_label.text() == "assets/v/v_anaconda"
    assert inspector.stat_tiles["sequences"].value.text() == "6"
    assert inspector.rederive_button.isHidden()  # not made by Retarget
    window.explorer.select("asset", "v_anaconda_hands")
    assert not inspector.rederive_button.isHidden()
    asked: list[str] = []
    inspector.reveal_requested.connect(asked.append)
    inspector.reveal_button.click()
    assert asked == ["v_anaconda_hands"]
    inspector.setCurrentWidget(inspector.attachments_page)  # nested in Animation
    assert inspector.currentWidget() is inspector.animation_tab
    inspector.notes.setText("tune the grip")
    inspector.notes.editingFinished.emit()
    assert project.assets["v_anaconda_hands"].notes == "tune the grip"


# -- log and feedback (phase 5) ----------------------------------------------
def test_log_levels_filters_and_asset_links(app) -> None:
    from PySide6.QtCore import QUrl

    from valve_qc_merger.studio.log_panel import LogPanel, classify
    assert classify("── Run view") == "header"
    assert classify("  warn: texture downscaled") == "warning"
    assert classify("error: model 'v_x' rejected") == "error"
    assert classify("retarget: 2 converted, 0 failed") == "info"  # "0 failed" is fine
    assert classify("  verify budgets  PASS") == "success"
    log = LogPanel()
    counts: list[tuple[int, int]] = []
    log.counts_changed.connect(lambda w, e: counts.append((w, e)))
    log.set_assets({"v_elite", "v_elite_hands"})
    for line in ("── Run", "v_elite_hands ok", "warn: big texture", "error: v_elite failed"):
        log.append_line(line)
    assert (log.warnings, log.errors) == (1, 1) and counts[-1] == (1, 1)
    assert 'href="asset:v_elite_hands"' in log.view.toHtml()  # longest name wins
    log.filter_buttons["error"].setChecked(True)
    assert log.shown_text() == "error: v_elite failed"
    log.filter_buttons["all"].setChecked(True)
    log.search.setText("texture")
    assert log.shown_text() == "warn: big texture"
    clicked: list[str] = []
    log.asset_clicked.connect(clicked.append)
    log._link(QUrl("asset:v_elite"))
    assert clicked == ["v_elite"]
    assert log.toPlainText().splitlines()[0] == "── Run"  # filters never drop lines
    log.clear()
    assert log.toPlainText() == "" and counts[-1] == (0, 0)


def test_log_hidden_until_a_job_fails(window) -> None:
    window.show()
    assert not window.log_dock.isVisible() and not window.log_button.isChecked()
    window._report_job("Run view", True, "", 2.0, 0, 0)
    assert window.toast.isVisible() and window.toast.property("level") == "success"
    assert not window.log_dock.isVisible()
    window._report_job("Run view", False, "merge failed: boom", 1.0, 0, 1)
    assert window.toast.property("level") == "error"
    assert "boom" in window.toast.detail.text()
    assert window.log_dock.isVisible() and window.log_button.isChecked()
    window.log.append_line("warn: something")
    assert window.log_button.text().startswith("Log") and "⚠ 1" in window.log_button.text()
    window.reset_layout()
    assert not window.log_dock.isVisible()
