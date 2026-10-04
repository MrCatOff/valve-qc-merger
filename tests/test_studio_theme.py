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
