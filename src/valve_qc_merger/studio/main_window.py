"""The studio main window: Explorer | viewport | Inspector, Log below."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolBar,
    QToolButton,
    QWidget,
)

from valve_qc_merger import __version__
from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.project import Project, ProjectError
from valve_qc_merger.services.base import Reporter
from valve_qc_merger.studio import help_dialogs, theme
from valve_qc_merger.studio.build_panel import BuildPanel, NewBuildDialog
from valve_qc_merger.studio.build_report import load_record
from valve_qc_merger.studio.dialogs import NewProjectDialog, SettingsDialog
from valve_qc_merger.studio.icons import ICON_SIZE, icon
from valve_qc_merger.studio.jobs import JobRunner
from valve_qc_merger.studio.log_panel import LogPanel, Toast
from valve_qc_merger.studio.model_info import ModelInfo, read_model_info
from valve_qc_merger.studio.scene import ModelScene, build_scene
from valve_qc_merger.studio.viewport import ViewportPanel
from valve_qc_merger.studio.welcome import WelcomePage
from valve_qc_merger.studio.widgets import (
    Explorer,
    ExplorerPanel,
    Inspector,
    project_title,
)

RECENT_LIMIT = 8
VIEW_MODEL_KINDS = frozenset({"v", "zhands"})  # what the game mirrors (cl_righthand)


class DockTitleBar(QWidget):
    """A dock's title: small caps label + close; double-click floats it."""

    def __init__(self, dock: QDockWidget) -> None:
        super().__init__(dock)
        self.dock = dock
        theme.set_role(self, "dock-bar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 4, 4)
        self.label = QLabel(dock.windowTitle().upper())
        theme.set_role(self.label, "dock-title")
        layout.addWidget(self.label)
        layout.addStretch(1)
        close = QToolButton()
        close.setIcon(icon("x", theme.TOKENS["muted"]))
        close.setIconSize(QSize(14, 14))
        close.setToolTip(f"Hide {dock.windowTitle()} (View menu shows it again)")
        close.setAutoRaise(True)
        close.clicked.connect(dock.close)
        layout.addWidget(close)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        self.dock.setFloating(not self.dock.isFloating())
        event.accept()


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None) -> None:
        super().__init__()
        self.settings = settings or QSettings("valve-qc-merger", "studio")
        self.project: Project | None = None
        self._info_cache: dict[str, ModelInfo] = {}
        self._pending_build = ""
        self._pending_asset = ""
        self._shown_asset = ""  # the asset the viewport shows ("" = none / an output)
        self.jobs = JobRunner(self)
        self.setWindowTitle(project_title(None))
        self.resize(1400, 860)

        self.viewport = ViewportPanel()
        self.welcome = WelcomePage(__version__)
        self.welcome.new_requested.connect(self.new_project)
        self.welcome.open_requested.connect(self.open_project_dialog)
        self.welcome.recent_requested.connect(lambda path: self.open_project(Path(path)))
        self.center = QStackedWidget()
        self.center.addWidget(self.welcome)
        self.center.addWidget(self.viewport)
        self.setCentralWidget(self.center)
        self._scene_cache: dict[str, ModelScene] = {}

        self.explorer = Explorer()
        self.inspector = Inspector()
        self.log = LogPanel()
        self.explorer_panel = ExplorerPanel(self.explorer)
        self._dock("Explorer", self.explorer_panel, Qt.DockWidgetArea.LeftDockWidgetArea, 300)
        self.build_panel = BuildPanel()
        self.right = QStackedWidget()
        self.right.addWidget(self.inspector)
        self.right.addWidget(self.build_panel)
        from valve_qc_merger.studio.sound_panel import SoundPanel
        self.sound_panel = SoundPanel()
        self.right.addWidget(self.sound_panel)
        from valve_qc_merger.studio.sprite_panel import SpritePanel
        self.sprite_panel = SpritePanel()
        self.right.addWidget(self.sprite_panel)
        self._dock("Inspector", self.right, Qt.DockWidgetArea.RightDockWidgetArea, 520)
        self._dock("Log", self.log, Qt.DockWidgetArea.BottomDockWidgetArea, 200)
        # the log stays out of the way until asked for (or a job fails)
        self.log_dock = self.docks[-1]
        self.log_dock.hide()
        self.log.asset_clicked.connect(self._reveal_asset)
        self.toast = Toast(self)
        self.toast.log_requested.connect(self.show_log)
        self._job_mark = (0.0, 0, 0)  # job start: time, warnings, errors

        self.counts_label = QLabel()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setVisible(False)
        self.cancel_button = QPushButton("Cancel")
        theme.set_role(self.cancel_button, "link")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.jobs.cancel)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.cancel_button)
        self.statusBar().addPermanentWidget(self.counts_label)
        self.log_button = QToolButton()
        self.log_button.setIcon(icon("square-terminal", theme.TOKENS["muted"]))
        self.log_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.log_button.setText("Log")
        self.log_button.setCheckable(True)
        self.log_button.setAutoRaise(True)
        self.log_button.setToolTip("Show / hide the log")
        self.log_button.clicked.connect(lambda on: self.log_dock.setVisible(on))
        self.statusBar().addPermanentWidget(self.log_button)
        self.statusBar().setSizeGripEnabled(False)

        self._build_menus()
        self._build_toolbar()
        self._build_view_menu()
        self.explorer.currentItemChanged.connect(lambda *_: self._update_actions())
        self.explorer.asset_selected.connect(self._select_asset)
        self.explorer.build_selected.connect(self._select_build)
        self.explorer.build_run_requested.connect(self.run_build)
        self.explorer.build_compile_requested.connect(self.compile_build)
        self.explorer.build_run_compile_requested.connect(
            lambda name: self.run_build(name, then_compile=True))
        self.explorer.build_delete_requested.connect(self.delete_build)
        self.viewport.compare_toggled.connect(self._compare)
        self.viewport.fov_changed.connect(self._fov_edited)
        self.viewport.right_hand_toggled.connect(self._right_hand_toggled)
        self.build_panel.run_requested.connect(self.run_build)
        self.build_panel.plan_requested.connect(self.plan_build)
        self.build_panel.deploy_requested.connect(self.deploy_build)
        self.explorer.build_deploy_requested.connect(self.deploy_build)
        self.build_panel.asset_requested.connect(self._reveal_asset)
        self.build_panel.compile_requested.connect(self.compile_build)
        self.build_panel.preview_requested.connect(self.preview_output)
        self.build_panel.preview_body_requested.connect(self.preview_body)
        self.build_panel.open_requested.connect(self.open_output)
        self.build_panel.changed.connect(lambda _n: self.explorer.show_project(self.project))
        self.explorer.remove_requested.connect(self.remove_asset)
        self.explorer.kind_change_requested.connect(self.set_kind)
        self.explorer.reveal_requested.connect(self._reveal)
        self.explorer.derive_requested.connect(self.derive_assets)
        self.explorer.category_move_requested.connect(self.move_to_category)
        self.explorer.category_new_requested.connect(self.new_category)
        self.explorer.category_rename_requested.connect(self.rename_category)
        self.explorer.category_delete_requested.connect(self.delete_category)
        self.explorer.category_builds_requested.connect(self.create_category_builds)
        self.explorer.rederive_requested.connect(self.rederive_asset)
        self.explorer.sound_selected.connect(self._select_sound)
        self.explorer.sound_import_requested.connect(self.import_sound_files)
        self.explorer.sound_fix_requested.connect(self.fix_sounds)
        self.explorer.sound_remove_requested.connect(self.remove_sound)
        self.explorer.sound_play_requested.connect(self.play_sound)
        self.sound_panel.fix_requested.connect(self.fix_sounds)
        self.sound_panel.undo_requested.connect(self.undo_sound_fix)
        self.sound_panel.remove_requested.connect(self.remove_sound)
        self.sound_panel.reveal_requested.connect(self._reveal_sound)
        self.sound_panel.asset_requested.connect(self._reveal_asset)
        self.sound_panel.precache_changed.connect(self._set_sound_precache)
        self.explorer.sprite_selected.connect(self._select_sprite)
        self.explorer.compare_requested.connect(self.compare_assets)
        self.explorer.sprite_import_requested.connect(self.import_sprites)
        self.explorer.sprite_new_requested.connect(self.new_sprite)
        self.explorer.hud_new_requested.connect(self.new_weapon_hud)
        self.explorer.sprite_remove_requested.connect(self.remove_sprite)
        self.sprite_panel.remove_requested.connect(self.remove_sprite)
        self.sprite_panel.reveal_requested.connect(self._reveal_sprite)
        self.inspector.kind_changed.connect(self.set_kind)
        self.inspector.notes_changed.connect(self._set_notes)
        self.inspector.retarget_requested.connect(lambda name: self.derive_assets([name]))
        self.inspector.rederive_requested.connect(lambda name: self.rederive_asset(name, False))
        self.inspector.reveal_requested.connect(self._reveal)
        bones = self.inspector.bones_page
        bones.bone_selected.connect(self.viewport.highlight_bone)
        bones.rename_requested.connect(self.rename_bone)
        bones.reparent_requested.connect(self.reparent_bone)
        bones.reparent_dialog_requested.connect(self.reparent_bone_dialog)
        bones.delete_requested.connect(self.delete_bone)
        bones.undo_requested.connect(self.undo_asset_edit)
        self.inspector.attachments_page.save_requested.connect(self.save_attachments)
        self.inspector.sequence_edit_requested.connect(self.edit_sequence)
        self.inspector.render_mode_edit_requested.connect(self.edit_render_mode)
        self.inspector.qc_page.save_requested.connect(self.save_qc)
        self.inspector.skins_page.skin_selected.connect(self.viewport.set_skin)
        self.viewport.skin_changed.connect(self._skin_shown)
        self.jobs.started.connect(self._job_started)
        # a job's log arrives line by line (a merge: thousands): batched into
        # the panel every 100 ms instead of one repaint per line
        self._log_buffer: list[str] = []
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(100)
        self._log_timer.timeout.connect(self._flush_log)
        self._log_timer.start()
        self.jobs.log.connect(self._log_buffer.append)
        self.jobs.progress.connect(self._job_progress)
        self.jobs.done.connect(self._job_done)
        self.log_dock.visibilityChanged.connect(self.log_button.setChecked)
        self.log.counts_changed.connect(self._log_counts)
        self._update_actions()
        self.center.setCurrentWidget(self.welcome)
        self._default_state = self.saveState()
        geometry = self.settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = self.settings.value("window/state")
        if state is not None:
            self.restoreState(state)

    # -- layout ------------------------------------------------------------
    def _dock(self, title: str, widget: QWidget, area: Qt.DockWidgetArea, size: int) -> None:
        dock = QDockWidget(title, self)
        dock.setObjectName(title)
        dock.setWidget(widget)
        dock.setTitleBarWidget(DockTitleBar(dock))
        self.docks = [*getattr(self, "docks", []), dock]
        self.addDockWidget(area, dock)
        horizontal = area in (Qt.DockWidgetArea.LeftDockWidgetArea,
                              Qt.DockWidgetArea.RightDockWidgetArea)
        self.resizeDocks([dock], [size], Qt.Orientation.Horizontal if horizontal
                         else Qt.Orientation.Vertical)

    def _build_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        self.act_new = file_menu.addAction("&New project…", self.new_project,
                                           QKeySequence.StandardKey.New)
        self.act_open = file_menu.addAction("&Open project…", self.open_project_dialog,
                                            QKeySequence.StandardKey.Open)
        self.recent_menu = file_menu.addMenu("Open &recent")
        self.act_close = file_menu.addAction("&Close project", self.close_project)
        file_menu.addSeparator()
        file_menu.addAction("&Quit", self.close, QKeySequence.StandardKey.Quit)

        project_menu = bar.addMenu("&Project")
        self.act_import_mdl = project_menu.addAction("Import .mdl files…", self.import_mdl_files,
                                                     QKeySequence("Ctrl+I"))
        self.act_import_mdl_dir = project_menu.addAction("Import .mdl folder…",
                                                         self.import_mdl_folder)
        self.act_import_dec = project_menu.addAction("Import decompiled folder…",
                                                     self.import_decompiled_folder)
        self.act_import_server = project_menu.addAction("Import server folder…",
                                                        self.import_server_folder)
        self.act_import_sounds = project_menu.addAction("Import sounds…",
                                                        self.import_sound_files)
        self.act_import_sound_dir = project_menu.addAction("Import sound folder…",
                                                           self.import_sound_folder)
        self.act_similar_sounds = project_menu.addAction("Find similar sounds…",
                                                         self.find_similar_sounds)
        project_menu.addSeparator()
        self.act_previews = project_menu.addAction("Weapon previews…", self.make_previews)
        self.act_server = project_menu.addAction("Server tools…", self.show_server,
                                                 QKeySequence("Ctrl+Shift+S"))
        self.act_settings = project_menu.addAction("Settings…", self.edit_settings)
        self.act_reveal = project_menu.addAction("Show project folder",
                                                 lambda: self._open_path(self.project.root))

        asset_menu = bar.addMenu("&Asset")
        self.act_derive = asset_menu.addAction(
            "Retarget…", lambda: self.derive_assets(self.explorer.selected_assets()),
            QKeySequence("Ctrl+R"))
        self.act_rederive = asset_menu.addAction(
            "Re-run retarget", lambda: self.rederive_asset(self.explorer.current_asset(), False),
            QKeySequence("Ctrl+Shift+R"))
        self.act_find = asset_menu.addAction("Find…", self._focus_filter,
                                             QKeySequence.StandardKey.Find)
        asset_menu.addSeparator()
        self.act_new_category = asset_menu.addAction("New category…", self.new_category)
        asset_menu.addSeparator()
        self.act_import_sprites = asset_menu.addAction("Import sprites…", self.import_sprites)
        self.act_new_sprite = asset_menu.addAction("New sprite from images…", self.new_sprite)
        self.act_new_hud = asset_menu.addAction("New weapon HUD…", self.new_weapon_hud)
        asset_menu.addSeparator()
        self.act_new_spray = asset_menu.addAction("New spray (tempdecal.wad)…", self.new_spray)
        self.act_open_wad = asset_menu.addAction("Open WAD…", self.open_wad)

        build_menu = bar.addMenu("&Build")
        self.act_new_build = build_menu.addAction("New build…", self.new_build,
                                                  QKeySequence("Ctrl+B"))
        self.act_run_build = build_menu.addAction(
            "Run selected build", lambda: self.run_build(self.explorer.current_build()),
            QKeySequence("F5"))
        self.act_plan_build = build_menu.addAction(
            "Plan selected build", lambda: self.plan_build(self.explorer.current_build()),
            QKeySequence("F6"))
        self.act_compile_build = build_menu.addAction(
            "Compile selected build",
            lambda: self.compile_build(self.explorer.current_build()), QKeySequence("F7"))
        self.act_run_compile_build = build_menu.addAction(
            "Run and compile selected build",
            lambda: self.run_build(self.explorer.current_build(), then_compile=True),
            QKeySequence("Shift+F5"))
        self.act_deploy_build = build_menu.addAction(
            "Deploy selected build to game",
            lambda: self.deploy_build(self.explorer.current_build()), QKeySequence("F8"))
        build_menu.addSeparator()
        self.act_export_package = build_menu.addAction(
            "Export server package…", self.export_package, QKeySequence("Ctrl+Shift+E"))
        build_menu.addSeparator()
        self.act_delete_build = build_menu.addAction(
            "Delete selected build…", lambda: self.delete_build(self.explorer.current_build()))
        self._refresh_recent()

    # (action attribute, icon, short toolbar label) — menus keep their long texts
    ACTION_ICONS = [
        ("act_new", "file-plus", "New"), ("act_open", "folder-open", "Open"),
        ("act_import_mdl", "file-down", "Import"), ("act_import_mdl_dir", "folder-input", ""),
        ("act_import_dec", "file-box", ""), ("act_settings", "settings", "Settings"),
        ("act_reveal", "folder-search", ""), ("act_derive", "hand", "Retarget"),
        ("act_rederive", "refresh-cw", "Re-run"), ("act_find", "search", "Find"),
        ("act_new_category", "folder-plus", ""), ("act_new_build", "package-plus", "New build"),
        ("act_plan_build", "list-checks", "Plan"), ("act_run_build", "play", "Run"),
        ("act_compile_build", "hammer", "Compile"), ("act_deploy_build", "rocket", "Deploy"),
        ("act_delete_build", "trash-2", ""), ("act_server", "gauge", "Server"),
        ("act_previews", "image", ""),
        ("act_export_package", "package", "Package"),
    ]
    TOOLBAR = ["act_import_mdl", "act_derive", "act_rederive", None, "act_new_build",
               "act_plan_build", "act_run_build", "act_compile_build", "act_deploy_build",
               None, "act_server", "stretch", "act_find", "act_settings"]

    def _build_toolbar(self) -> None:
        for attr, name, label in self.ACTION_ICONS:
            action: QAction = getattr(self, attr)
            action.setIcon(icon(name))
            if label:
                action.setIconText(label)
            keys = action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
            text = action.text().replace("&", "").rstrip("…")
            action.setToolTip(f"{text} ({keys})" if keys else text)
        bar = QToolBar("Toolbar")
        bar.setObjectName("Toolbar")
        bar.setMovable(False)
        bar.setIconSize(ICON_SIZE)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for attr in self.TOOLBAR:
            if attr is None:
                bar.addSeparator()
            elif attr == "stretch":
                spacer = QWidget()
                spacer.setSizePolicy(QSizePolicy.Policy.Expanding,
                                     QSizePolicy.Policy.Preferred)
                bar.addWidget(spacer)
            else:
                bar.addAction(getattr(self, attr))
        run = bar.widgetForAction(self.act_run_build)
        self.act_run_build.setIcon(icon("play", theme.TOKENS["on_accent"]))
        theme.set_primary(run)
        for attr in ("act_find", "act_settings"):
            bar.widgetForAction(getattr(self, attr)).setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.toolbar = bar
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)

    def _build_view_menu(self) -> None:
        menu = self.menuBar().addMenu("&View")
        menu.addAction(self.toolbar.toggleViewAction())
        for dock in self.docks:
            menu.addAction(dock.toggleViewAction())
        menu.addSeparator()
        self.act_reset_layout = menu.addAction(icon("layout-dashboard"), "Reset layout",
                                               self.reset_layout)
        help_menu = self.menuBar().addMenu("&Help")
        self.act_shortcuts = help_menu.addAction(icon("info"), "Keyboard shortcuts",
                                                 self.show_shortcuts, QKeySequence("F1"))
        self.act_docs = help_menu.addAction(
            icon("circle-help"), "Documentation",
            lambda: QDesktopServices.openUrl(QUrl(help_dialogs.DOCS_URL)))
        self.act_graphics = help_menu.addAction("Graphics report…", self.graphics_report)
        help_menu.addSeparator()
        self.act_about = help_menu.addAction("About valve-qc-merger Studio", self.show_about)

    def import_server_folder(self) -> None:
        """Project ▸ Import server folder: models + the sounds they play + the
        weapon HUDs of a mod folder, as one background job."""
        from valve_qc_merger.studio.dialogs import ImportServerDialog
        project = self.project
        if project is None or self.jobs.busy:
            return
        dialog = ImportServerDialog(project.settings.game_dir or "",
                                    sorted(project.categories, key=str.lower), self)
        if dialog.exec() != ImportServerDialog.DialogCode.Accepted:
            return
        root = Path(dialog.folder_edit.text().strip())
        if not root.is_dir():
            QMessageBox.warning(self, "Import server folder", f"{root} is not a folder.")
            return
        options = {"models": dialog.models_box.isChecked(),
                   "sounds": dialog.sounds_box.currentData(),
                   "sprites": dialog.sprites_box.isChecked(),
                   "category": dialog.category_box.currentData()}

        self._start_task(f"Import {root.name}", "import_server", folder=str(root),
                         options=options)

    def make_previews(self) -> None:
        """Project ▸ Weapon previews: a PNG per view model without the hands,
        a grid sheet and an HTML catalog, as one background job."""
        from valve_qc_merger.preview.catalog import build_catalog, preview_assets
        from valve_qc_merger.preview.render import Options
        from valve_qc_merger.studio.dialogs import PreviewDialog
        project = self.project
        if project is None or self.jobs.busy:
            return
        categories = sorted(project.categories, key=str.lower)
        counts: dict[str | None, int] = {None: len(preview_assets(project))}
        for category in categories:
            counts[category] = len(preview_assets(project, category))
        if not counts[None]:
            QMessageBox.information(self, "Weapon previews",
                                    "The project has no view models (v_) yet.")
            return
        dialog = PreviewDialog(str(project.root / "previews"), categories, counts, self)
        if dialog.exec() != PreviewDialog.DialogCode.Accepted:
            return
        folder = Path(dialog.folder_edit.text().strip() or project.root / "previews")
        assets = preview_assets(project, dialog.category_box.currentData())
        width, height = dialog.size_box.currentData()
        options = Options(width=width, height=height, barrel_left=dialog.left_box.isChecked())
        columns = dialog.columns_spin.value() if dialog.sheet_box.isChecked() else 0

        def work(reporter: Reporter) -> str:
            catalog = build_catalog(project, assets, folder, options, reporter=reporter)
            for line in catalog.failed:
                reporter.log(f"  warn: {line}")
            for entry in catalog.entries:
                if entry.hands == "kept":
                    reporter.log(f"  warn: {entry.asset}: drawn with its hands (none found "
                                 "to leave out, or the weapon is the hands)")
            if columns and catalog.previews:
                from valve_qc_merger.studio.preview_sheet import sheet
                sheet(catalog.previews, [e.name for e in catalog.entries],
                      columns).save(str(folder / "sheet.png"))
            reporter.log(f"  {len(catalog.entries)} preview(s) → {folder}")
            return str(folder)

        self.jobs.start("Weapon previews", work)
        self._open_after_job = folder

    # -- sounds ----------------------------------------------------------------
    def import_sound_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import sounds", self.settings.value("last_sound_import", str(Path.home())),
            "WAV sounds (*.wav)")
        if files:
            self.settings.setValue("last_sound_import", str(Path(files[0]).parent))
            self._import_sounds([Path(f) for f in files])

    def import_sound_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Import every WAV of a folder (paths after sound/ are kept)",
            self.settings.value("last_sound_import", str(Path.home())))
        if folder:
            self.settings.setValue("last_sound_import", folder)
            self._import_sounds([Path(folder)])

    def _import_sounds(self, sources: list[Path]) -> None:
        from valve_qc_merger.project import sounds
        if self.project is None:
            return
        names = sounds.import_sounds(self.project, sources)
        self.log.append_line(f"imported {len(names)} sound(s)")
        self.explorer.show_project(self.project)
        if names:
            self.explorer.select("sound", names[0])
            self.toast.show_message("success", f"{len(names)} sound(s) imported",
                                    "named by their path after sound/")

    def find_similar_sounds(self) -> None:
        """Project ▸ Find similar sounds: share one file between sounds that
        are the same (or close), stock ones first."""
        from valve_qc_merger.studio.similar_sounds import SimilarSoundsDialog
        if self.project is None:
            return
        SimilarSoundsDialog(self.project, self).exec()
        window = getattr(self, "server_window", None)
        if window is not None and window.isVisible():
            window.recount_sounds()

    def _select_sound(self, name: str) -> None:
        from valve_qc_merger.project import sounds
        if self.project is None or not name:
            return
        self.right.setCurrentWidget(self.sound_panel)
        path = sounds.sound_path(self.project, name)
        users = sounds.sound_users(self.project).get(name.lower(), [])
        automatic = sounds.auto_precache(self.project, name)
        precache = None if automatic is None else (
            self.project.settings.sound_precache.get(sounds.sound_key(name), "auto"),
            automatic)
        self.sound_panel.show_sound(name, path if path.is_file() else None, users,
                                    sounds.can_undo_fix(self.project, name), precache)

    def _set_sound_precache(self, name: str, choice: str) -> None:
        from valve_qc_merger.project import sounds
        if self.project is None:
            return
        sounds.set_precache(self.project, name, choice)
        self.log.append_line(f"sound/{name}: precache {choice}")
        window = getattr(self, "server_window", None)
        if window is not None and window.isVisible():
            window.recount_sounds()

    def fix_sounds(self, names: list[str]) -> None:
        from valve_qc_merger.project import sounds
        from valve_qc_merger.sound.wav import WavError
        from valve_qc_merger.studio.sound_panel import FixDialog
        if self.project is None or not names:
            return
        dialog = FixDialog(names, self)
        if dialog.exec() != FixDialog.DialogCode.Accepted:
            return
        options = dialog.options()
        failed = []
        for name in names:
            try:
                sounds.fix_sound(self.project, name, options)
            except (WavError, OSError, ValueError) as exc:
                failed.append(f"{name}: {exc}")
        for line in failed:
            self.log.append_line(f"warn: fix {line}")
        self.log.append_line(f"fixed {len(names) - len(failed)} sound(s)")
        self.explorer.show_project(self.project)
        self.explorer.select("sound", names[0])
        self._select_sound(names[0])
        self.toast.show_message("warning" if failed else "success",
                                f"{len(names) - len(failed)} of {len(names)} sound(s) fixed",
                                failed[0] if failed else "the originals are kept: Undo fix")

    def undo_sound_fix(self, name: str) -> None:
        from valve_qc_merger.project import sounds
        if self.project is None:
            return
        sounds.undo_fix(self.project, name)
        self.explorer.show_project(self.project)
        self.explorer.select("sound", name)
        self._select_sound(name)

    def remove_sound(self, name: str) -> None:
        from valve_qc_merger.project import sounds
        if self.project is None:
            return
        answer = QMessageBox.question(self, "Remove sound",
                                      f"Remove sound/{name} from the project's library?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        sounds.remove_sound(self.project, name)
        self.log.append_line(f"removed sound/{name}")
        self.explorer.show_project(self.project)

    def play_sound(self, name: str) -> None:
        from valve_qc_merger.project import sounds
        from valve_qc_merger.studio import audio
        if self.project is not None:
            path = sounds.resolve(self.project, name)
            if path is not None:
                audio.play(path)

    def _reveal_sound(self, name: str) -> None:
        from valve_qc_merger.project import sounds
        if self.project is not None:
            self._open_path(sounds.sound_path(self.project, name).parent)

    def compare_assets(self, name: str, other: str) -> None:
        """Explorer ▸ Compare with: B flips the viewport between ``name`` and
        ``other`` (same camera, sequence by name, frame)."""
        if self.project is None:
            return
        self._compare_pair = (name, other)
        self.explorer.select("asset", name)
        self._select_asset(name)
        self.viewport.set_compare(other, "other")
        self.statusBar().showMessage(f"B flips between {name} and {other}", 6000)

    # -- sprites ---------------------------------------------------------------
    def _select_sprite(self, name: str) -> None:
        from valve_qc_merger.project import sprites
        if self.project is None or not name:
            return
        self.right.setCurrentWidget(self.sprite_panel)
        self.sprite_panel.show_sprite(name, sprites.sprite_path(self.project, name),
                                      sprites.sprites_dir(self.project))

    def import_sprites(self) -> None:
        from valve_qc_merger.project import sprites
        if self.project is None:
            return
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import sprites / HUD files",
            self.settings.value("last_sprite_import", str(Path.home())),
            "Sprites and HUD files (*.spr *.txt)")
        if not files:
            return
        self.settings.setValue("last_sprite_import", str(Path(files[0]).parent))
        names = sprites.import_sprites(self.project, [Path(f) for f in files])
        self.log.append_line(f"imported {len(names)} sprite file(s)")
        self.explorer.show_project(self.project)
        if names:
            self.explorer.select("sprite", names[0])

    def new_sprite(self) -> None:
        from valve_qc_merger.project import sprites
        from valve_qc_merger.studio.sprite_panel import NewSpriteDialog, load_rgba
        if self.project is None:
            return
        dialog = NewSpriteDialog(self)
        if dialog.exec() != NewSpriteDialog.DialogCode.Accepted or not dialog.frame_paths():
            return
        try:
            images = [load_rgba(p) for p in dialog.frame_paths()]
            sizes = {im.shape[:2] for im in images}
            if len(sizes) > 1:
                raise ValueError("every frame must have the same size")
            name = sprites.make_sprite(self.project, dialog.name_edit.text().strip().strip("/"),
                                       images, fmt=dialog.format_box.currentData(),
                                       stype=dialog.type_box.currentData())
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "New sprite", str(exc))
            return
        self.log.append_line(f"made sprites/{name}")
        self.explorer.show_project(self.project)
        self.explorer.select("sprite", name)

    def new_spray(self) -> None:
        """Asset ▸ New spray: an image -> tempdecal.wad (needs no project)."""
        from valve_qc_merger.studio.wad_tools import SprayDialog
        folder = self.project.settings.game_dir if self.project is not None else ""
        SprayDialog(folder or "", self).exec()

    def open_wad(self) -> None:
        """Asset ▸ Open WAD: browse the textures of a WAD3."""
        from valve_qc_merger.studio.wad_tools import WadViewer
        path, _ = QFileDialog.getOpenFileName(
            self, "Open WAD", self.settings.value("last_wad", str(Path.home())),
            "WAD3 (*.wad)")
        if path:
            self.settings.setValue("last_wad", str(Path(path).parent))
            WadViewer(path, self).exec()

    def new_weapon_hud(self) -> None:
        from valve_qc_merger.project import sprites
        from valve_qc_merger.studio.sprite_panel import WeaponHudDialog, load_rgba
        if self.project is None:
            return
        dialog = WeaponHudDialog(self)
        if dialog.exec() != WeaponHudDialog.DialogCode.Accepted:
            return
        weapon = dialog.weapon_edit.text().strip()
        try:
            if not weapon or weapon == "weapon_" or not dialog.icon_edit.text().strip():
                raise ValueError("name the weapon and choose its icon")
            icon_image = load_rgba(Path(dialog.icon_edit.text().strip()))
            selected = (load_rgba(Path(dialog.selected_edit.text().strip()))
                        if dialog.selected_edit.text().strip() else None)
            ammo = (load_rgba(Path(dialog.ammo_edit.text().strip()))
                    if dialog.ammo_edit.text().strip() else None)
            names = sprites.make_weapon_hud(self.project, weapon, icon_image,
                                            selected=selected, ammo=ammo)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "New weapon HUD", str(exc))
            return
        self.log.append_line("made " + ", ".join(f"sprites/{n}" for n in names))
        self.explorer.show_project(self.project)
        self.explorer.select("sprite", names[0])

    def remove_sprite(self, name: str) -> None:
        from valve_qc_merger.project import sprites
        if self.project is None:
            return
        answer = QMessageBox.question(self, "Remove sprite",
                                      f"Remove sprites/{name} from the project?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        sprites.remove_sprite(self.project, name)
        self.explorer.show_project(self.project)

    def _reveal_sprite(self, name: str) -> None:
        from valve_qc_merger.project import sprites
        if self.project is not None:
            self._open_path(sprites.sprite_path(self.project, name).parent)

    def export_package(self) -> None:
        """Build ▸ Export server package: compiled builds + their sounds as a
        cstrike/ tree (server and FastDL), an AMXX include, a .res list and a
        size report."""
        import re as _re

        from PySide6.QtWidgets import QApplication

        from valve_qc_merger.server.package import export_package
        if self.project is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Export the server package into",
            self.settings.value("last_package", str(self.project.root.parent)))
        if not folder:
            return
        self.settings.setValue("last_package", folder)
        slug = _re.sub(r"[^0-9A-Za-z]+", "_", self.project.name).strip("_").lower() or "pack"
        out = Path(folder) / f"{slug}_server_package"
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = export_package(self.project, out)
        except OSError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Export server package", str(exc))
            return
        QApplication.restoreOverrideCursor()
        from valve_qc_merger.server.package import report
        for line in report(result).splitlines():
            if line.strip():
                self.log.append_line(line if not line.startswith("  sound/")
                                     else f"warn: missing {line.strip()}")
        detail = f"{len(result.files)} files, {result.total / 1048576:.1f} MB → {out}"
        versions = result.versions
        if versions is not None and not versions.first:
            detail += (f"; update/ holds {len(versions.upload)} new or changed file(s)"
                       if versions.upload else "; nothing changed since the last export")
        if result.missing_sounds or result.skipped_builds:
            self.toast.show_message("warning", "Server package exported with gaps",
                                    f"{detail}; see the log for what is missing")
        else:
            self.toast.show_message("success", "Server package exported", detail)
        self._open_path(out)

    def show_server(self) -> None:
        """Project ▸ Server: precache budget per map + the mod-folder doctor
        (one window, refreshed every time it is shown)."""
        from valve_qc_merger.studio.server_window import ServerWindow
        if self.project is None:
            return
        window = getattr(self, "server_window", None)
        if window is None or window.project is not self.project:
            if window is not None:
                window.close()
            window = ServerWindow(self.project, self)
            self.server_window = window
        else:
            window.refresh()
        window.show()
        window.raise_()
        window.activateWindow()

    def show_shortcuts(self) -> None:
        help_dialogs.ShortcutsDialog(self.menuBar(), self).exec()

    def show_about(self) -> None:
        help_dialogs.AboutDialog(self).exec()

    def reset_layout(self) -> None:
        for dock in self.docks:
            dock.setFloating(False)
        self.restoreState(self._default_state)
        for dock in self.docks:
            dock.setVisible(dock is not self.log_dock)
        self.toolbar.show()

    def show_log(self) -> None:
        self.log_dock.show()
        self.log_dock.raise_()

    def _log_counts(self, warnings: int, errors: int) -> None:
        parts = ["Log"] + ([f"⚠ {warnings}"] if warnings else []) + \
            ([f"✗ {errors}"] if errors else [])
        self.log_button.setText("   ".join(parts))
        self.log_button.setToolTip(f"Show / hide the log ({warnings} warning(s), "
                                   f"{errors} error(s))")

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        super().resizeEvent(event)
        if self.toast.isVisible():
            self.toast.place()

    def _update_actions(self) -> None:
        has = self.project is not None
        idle = not self.jobs.busy
        for action in (self.act_import_mdl, self.act_import_mdl_dir, self.act_import_dec,
                       self.act_settings, self.act_reveal, self.act_close, self.act_find,
                       self.act_new_category, self.act_new_build, self.act_server,
                       self.act_previews,
                       self.act_import_sounds, self.act_import_sound_dir,
                       self.act_similar_sounds,
                       self.act_export_package, self.act_import_sprites,
                       self.act_new_sprite, self.act_new_hud, self.act_import_server):
            action.setEnabled(has and idle)
        # what acts on the selection is enabled only when there is one
        asset = has and bool(self.explorer.selected_assets())
        build = has and bool(self.explorer.current_build())
        for action in (self.act_derive, self.act_rederive):
            action.setEnabled(asset and idle)
        for action in (self.act_run_build, self.act_compile_build, self.act_plan_build,
                       self.act_deploy_build, self.act_run_compile_build,
                       self.act_delete_build):
            action.setEnabled(build and idle)
        for action in (self.act_new, self.act_open):
            action.setEnabled(idle)
        self.recent_menu.setEnabled(idle)
        self._update_counts()

    def _update_counts(self) -> None:
        project = self.project
        if project is None:
            self.counts_label.setText("No project")
            self.log.set_assets(frozenset())
            return
        self.log.set_assets(frozenset(project.assets))
        assets, builds = len(project.assets), len(project.builds)
        self.counts_label.setText(f"{project.name}  ·  {assets} asset{'s' * (assets != 1)}"
                                  f"  ·  {builds} build{'s' * (builds != 1)}")

    # -- project lifecycle -------------------------------------------------
    def new_project(self) -> None:
        dialog = NewProjectDialog(self, Path(self.settings.value("last_folder", str(Path.home()))))
        if dialog.exec() != NewProjectDialog.DialogCode.Accepted:
            return
        root, name = dialog.target()
        try:
            project = Project.create(root, name)
            from valve_qc_merger.project.workflow import apply_template
            apply_template(project, dialog.template())
        except (ProjectError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "New project", str(exc))
            return
        self.settings.setValue("last_folder", str(root.parent))
        self.set_project(project)

    def open_project_dialog(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Open project folder", self.settings.value("last_folder", str(Path.home())))
        if folder:
            self.open_project(Path(folder))

    def open_project(self, root: Path) -> bool:
        try:
            project = Project.open(root)
        except (ProjectError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "Open project", f"{root}\n\n{exc}")
            return False
        self.settings.setValue("last_folder", str(root.parent))
        self.set_project(project)
        return True

    def close_project(self) -> None:
        self.set_project(None)

    def set_project(self, project: Project | None) -> None:
        window = getattr(self, "server_window", None)
        if window is not None:  # it belongs to the previous project
            window.close()
            self.server_window = None
        self.project = project
        self._info_cache.clear()
        self._scene_cache.clear()
        self.viewport.set_scene(None)
        self.setWindowTitle(project_title(project))
        self.explorer.show_project(project)
        self.inspector.show_asset(project, None)
        self.center.setCurrentWidget(self.viewport if project is not None else self.welcome)
        if project is not None:
            self._remember(project.root)
            self.log.append_line(f"opened {project.root}")
        self._update_actions()

    def _remember(self, root: Path) -> None:
        recent = [p for p in self._recent() if Path(p) != root]
        recent.insert(0, str(root))
        self.settings.setValue("recent", recent[:RECENT_LIMIT])
        self._refresh_recent()

    def _recent(self) -> list[str]:
        value = self.settings.value("recent", [])
        if isinstance(value, str):
            value = [value]
        return [str(v) for v in value or []]

    def _refresh_recent(self) -> None:
        self.recent_menu.clear()
        recent = [p for p in self._recent() if (Path(p) / "project.toml").exists()]
        for path in recent:
            self.recent_menu.addAction(path, lambda p=path: self.open_project(Path(p)))
        if not recent:
            self.recent_menu.addAction("(none)").setEnabled(False)
        if hasattr(self, "welcome"):
            self.welcome.set_recent(recent)

    # -- assets ------------------------------------------------------------
    def import_mdl_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import models", self.settings.value("last_import", str(Path.home())),
            "GoldSource models (*.mdl)")
        if files:
            self.settings.setValue("last_import", str(Path(files[0]).parent))
            self._run_import("Import .mdl", [Path(f) for f in files], mdl=True)

    def import_mdl_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Import every .mdl in a folder",
            self.settings.value("last_import", str(Path.home())))
        if folder:
            self.settings.setValue("last_import", folder)
            self._run_import("Import .mdl folder", [Path(folder)], mdl=True)

    def import_decompiled_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Import decompiled model(s): a folder with a .qc, or a folder of them",
            self.settings.value("last_import", str(Path.home())))
        if folder:
            self.settings.setValue("last_import", folder)
            self._run_import("Import decompiled", [Path(folder)], mdl=False)

    AUTO_CATEGORY = "(automatic: follow the weapon's other models)"

    def _ask_import_category(self) -> str | None:
        """Category for an import: "" = automatic, None = cancelled. Skipped
        (automatic) while the project has no categories."""
        from PySide6.QtWidgets import QInputDialog
        project = self.project
        if project is None or not project.categories:
            return ""
        choices = [self.AUTO_CATEGORY] + sorted(project.categories, key=str.lower)
        current = self.explorer.current_category()
        index = choices.index(current) if current in choices else 0
        text, ok = QInputDialog.getItem(
            self, "Import", "Category (pick one or type a new name):", choices, index, True)
        if not ok:
            return None
        text = text.strip()
        return "" if text in ("", self.AUTO_CATEGORY) else text

    def _run_import(self, title: str, sources: list[Path], *, mdl: bool) -> None:
        project = self.project
        assert project is not None
        category = self._ask_import_category()
        if category is None:
            return

        self._start_task(title, "import_models", sources=[str(s) for s in sources],
                         category=category or None, mdl=mdl)

    def remove_asset(self, name: str) -> None:
        if self.project is None or self.jobs.busy:
            return
        answer = QMessageBox.question(
            self, "Remove asset",
            f"Remove {name} from the project and delete its folder?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._shown_asset == name:  # nothing of it stays on screen
            self.viewport.set_scene(None)
            self._shown_asset = ""
        leftovers = self.project.remove_asset(name)
        self._info_cache.pop(name, None)
        self._scene_cache.pop(name, None)
        self.log.append_line(f"removed {name}")
        for folder in leftovers:
            self.log.append_line(f"warn: could not delete {folder} (a file in it is open "
                                 "in another program?) — delete it by hand")
        if leftovers:
            self.toast.show_message("warning", f"{name} removed, its folder was not",
                                    f"{leftovers[0]} is in use; delete it by hand")
        self.explorer.show_project(self.project)

    def set_kind(self, name: str, kind: str) -> None:
        if self.project is None or self.jobs.busy or self.project.assets[name].kind == kind:
            return
        self.project.set_kind(name, kind)
        self._info_cache.pop(name, None)
        self._scene_cache.pop(name, None)
        self.log.append_line(f"{name}: kind -> {kind}")
        self.explorer.show_project(self.project)
        self._select_asset(name)

    def _set_notes(self, name: str, notes: str) -> None:
        if self.project is not None and name in self.project.assets:
            self.project.assets[name].notes = notes
            self.project.save()

    # -- bone tools ----------------------------------------------------------
    def _edit_asset(self, title: str, edit) -> None:  # noqa: ANN001 - callable
        """Snapshot the selected asset, run ``edit(asset_dir)`` as a job,
        then reload it (Inspector + viewport)."""
        project, name = self.project, self.explorer.current_asset()
        if project is None or not name or self.jobs.busy:
            return
        directory = project.asset_dir(name)

        def work(reporter: Reporter) -> object:
            snapshot = project.snapshot_asset(name)
            try:
                result = edit(directory)
            except Exception:
                # nothing changed: drop the snapshot, or Undo would be a no-op
                import shutil
                shutil.rmtree(snapshot, ignore_errors=True)
                raise
            for warning in result.warnings:
                reporter.log(f"  warn: {warning}")
            reporter.log(f"  {name}: {len(result.files)} file(s) rewritten, max pose "
                         f"deviation {result.max_pose_deviation:.2e}u")
            return result

        self._pending_asset = name
        self.jobs.start(f"{title} ({name})", work)

    def rename_bone(self, bone: str) -> None:
        from PySide6.QtWidgets import QInputDialog

        from valve_qc_merger.project.bones import rename_bone
        new, ok = QInputDialog.getText(self, "Rename bone", f"New name for {bone}:", text=bone)
        if ok and new.strip() and new.strip() != bone:
            self._edit_asset("Rename bone", lambda d: rename_bone(d, bone, new.strip()))

    def reparent_bone(self, bone: str, parent: object) -> None:
        from valve_qc_merger.project.bones import reparent
        self._edit_asset("Reparent bone", lambda d: reparent(d, bone, parent))

    def reparent_bone_dialog(self, bone: str) -> None:
        from PySide6.QtWidgets import QInputDialog

        from valve_qc_merger.studio.bone_tools import ROOT_CHOICE
        info = self._info_cache.get(self.explorer.current_asset())
        if info is None:
            return
        # a bone cannot move under itself or its descendants
        children: dict[int, list[int]] = {}
        for b in info.bones:
            children.setdefault(b.parent, []).append(b.index)
        start = next(b.index for b in info.bones if b.name == bone)
        banned, stack = set(), [start]
        while stack:
            index = stack.pop()
            banned.add(index)
            stack.extend(children.get(index, []))
        choices = [ROOT_CHOICE] + [b.name for b in info.bones if b.index not in banned]
        parent, ok = QInputDialog.getItem(self, "Change parent", f"New parent of {bone}:",
                                          choices, 0, False)
        if ok:
            self.reparent_bone(bone, None if parent == ROOT_CHOICE else parent)

    def delete_bone(self, bone: str) -> None:
        from valve_qc_merger.project.bones import delete_bone
        answer = QMessageBox.question(
            self, "Delete bone",
            f"Delete {bone}? Its children keep their pose; vertices it carries move to "
            "its parent and stop following its own motion. (Undo restores it.)")
        if answer == QMessageBox.StandardButton.Yes:
            self._edit_asset("Delete bone", lambda d: delete_bone(d, bone))

    def save_attachments(self, specs: list) -> None:
        from valve_qc_merger.project.bones import write_attachments
        self._edit_asset("Save attachments", lambda d: write_attachments(d, specs))

    # -- QC edits ----------------------------------------------------------------
    def edit_sequence(self, row: int) -> None:
        from valve_qc_merger.project.qc_edit import parse_blocks, qc_file, set_sequence
        from valve_qc_merger.studio.qc_tools import SequenceDialog
        project, name = self.project, self.explorer.current_asset()
        if project is None or not name or self.jobs.busy:
            return
        info = self._info_cache.get(name)
        if info is None or not 0 <= row < len(info.sequences):
            return
        wanted = info.sequences[row].name
        blocks = parse_blocks(qc_file(project.asset_dir(name)).read_text(encoding="latin-1"))
        index = next((i for i, b in enumerate(blocks) if b.name == wanted), None)
        if index is None:
            return
        dialog = SequenceDialog(blocks[index], info.sequences[row].frames or None, self)
        if dialog.exec() != SequenceDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        self._edit_asset("Edit sequence", lambda d: set_sequence(d, index, **values))

    def edit_render_mode(self, texture: str) -> None:
        from PySide6.QtWidgets import QInputDialog

        from valve_qc_merger.project.qc_edit import RENDER_MODES, set_render_mode
        info = self._info_cache.get(self.explorer.current_asset())
        current = next((t.render_mode for t in info.textures if t.name == texture), "") \
            if info is not None else ""
        choices = ["normal"] + list(RENDER_MODES)
        mode, ok = QInputDialog.getItem(
            self, "Render mode", f"{texture}:", choices,
            choices.index(current) if current in choices else 0, False)
        if ok and mode != (current or "normal"):
            self._edit_asset("Render mode", lambda d: set_render_mode(
                d, texture, None if mode == "normal" else mode))

    def save_qc(self, text: str) -> None:
        from valve_qc_merger.project.qc_edit import write_qc
        self._edit_asset("Save QC", lambda d: write_qc(d, text))

    def _skin_shown(self, index: int) -> None:
        table = self.inspector.skins_page.table
        if table.currentRow() != index and index < table.rowCount():
            table.blockSignals(True)
            table.selectRow(index)
            table.blockSignals(False)

    def undo_asset_edit(self) -> None:
        name = self.explorer.current_asset()
        if self.project is None or not name or self.jobs.busy:
            return
        if self.project.undo_asset(name):
            self.log.append_line(f"{name}: last edit undone")
            self._info_cache.pop(name, None)
            self._scene_cache.pop(name, None)
            self._select_asset(name)

    # -- categories ----------------------------------------------------------
    def _focus_filter(self) -> None:
        self.explorer_panel.search.setFocus()
        self.explorer_panel.search.selectAll()

    def _ask_category_name(self, title: str, text: str = "") -> str | None:
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, title, "Category name:", text=text)
        return name.strip() if ok and name.strip() else None

    def new_category(self) -> None:
        if self.project is None or self.jobs.busy:
            return
        name = self._ask_category_name("New category")
        if name is None:
            return
        try:
            self.project.add_category(name)
        except ProjectError as exc:
            QMessageBox.warning(self, "New category", str(exc))
            return
        self.explorer.show_project(self.project)

    def move_to_category(self, names: list[str], category: str) -> None:
        """File assets under a category; offers to bring the weapon's other
        models (v_/p_/w_, derived) along."""
        from valve_qc_merger.studio.widgets import NEW_CATEGORY, UNCATEGORIZED
        project = self.project
        if project is None or self.jobs.busy or not names:
            return
        if category == NEW_CATEGORY:
            category = self._ask_category_name("New category")
            if category is None:
                return
        target = category.strip().lower()
        extra = sorted({s for n in names for s in project.siblings(n)
                        if s not in names and project.assets[s].category.lower() != target})
        with_siblings = False
        if extra:
            shown = ", ".join(extra[:8]) + ("…" if len(extra) > 8 else "")
            answer = QMessageBox.question(
                self, "Move to category",
                f"Also move the same weapon's other models?\n\n{shown}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Yes)
            if answer == QMessageBox.StandardButton.Cancel:
                return
            with_siblings = answer == QMessageBox.StandardButton.Yes
        try:
            moved = project.set_category(names, category, with_siblings=with_siblings)
        except ProjectError as exc:
            QMessageBox.warning(self, "Move to category", str(exc))
            return
        self.log.append_line(f"{len(moved)} asset(s) -> {category or UNCATEGORIZED}: "
                             f"{', '.join(moved)}")
        self.explorer.show_project(project)
        if self.explorer.current_asset():
            self._select_asset(self.explorer.current_asset())

    def rename_category(self, old: str) -> None:
        if self.project is None or self.jobs.busy:
            return
        new = self._ask_category_name("Rename category", old)
        if new is None or new == old:
            return
        try:
            self.project.rename_category(old, new)
        except ProjectError as exc:
            QMessageBox.warning(self, "Rename category", str(exc))
            return
        self.explorer.show_project(self.project)

    def delete_category(self, name: str) -> None:
        if self.project is None or self.jobs.busy:
            return
        count = len(self.project.category_assets(name))
        answer = QMessageBox.question(
            self, "Delete category",
            f"Delete category {name}? Its {count} asset(s) stay in the project, "
            "uncategorized; builds that used it take every asset of their kind.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.project.remove_category(name)
        self.explorer.show_project(self.project)

    def create_category_builds(self, category: str) -> None:
        if self.project is None or self.jobs.busy:
            return
        try:
            made = self.project.create_category_builds(category)
        except ProjectError as exc:
            QMessageBox.warning(self, "Create builds", str(exc))
            return
        if made:
            self.log.append_line(f"{category}: builds " + ", ".join(
                f"{b.name} ({b.kind})" for b in made))
        else:
            self.statusBar().showMessage(f"{category}: its builds already exist", 5000)
        self.explorer.show_project(self.project)
        if made:
            self.explorer.select("build", made[0].name)

    # -- retarget (derived assets) --------------------------------------------
    def derive_assets(self, names: list[str]) -> None:
        """Retarget dialog over the selected asset(s) (modeless: Apply keeps it
        open for the tuning loop, Run closes it)."""
        from valve_qc_merger.studio.derive_dialog import DeriveDialog
        project = self.project
        if project is None or self.jobs.busy:
            return
        names = [n for n in names if n in project.assets]
        if not names:
            self.statusBar().showMessage("select an asset in the Explorer", 4000)
            return
        self._open_derive_dialog(DeriveDialog(
            names, self, existing=set(project.assets),
            kinds={project.assets[n].kind for n in names}))

    def rederive_asset(self, name: str, edit: bool) -> None:
        """Re-run a derived asset from its source; ``edit`` opens its settings first."""
        from valve_qc_merger.studio.derive_dialog import DeriveDialog
        project = self.project
        if project is None or self.jobs.busy or not name:
            return
        asset = project.assets.get(name)
        if asset is None or not asset.derived:
            self.statusBar().showMessage(f"{name} was not made by Retarget", 4000)
            return
        derived = asset.derived
        if derived["from"] not in project.assets:
            QMessageBox.warning(self, "Retarget",
                                f"The source asset {derived['from']} is no longer in the project.")
            return
        options = dict(derived.get("options", {}))
        if edit:
            self._open_derive_dialog(DeriveDialog([derived["from"]], self,
                                                  mode=derived["mode"], options=options,
                                                  name=name))
            return
        self._run_derive([(derived["from"], name)], derived["mode"], options)

    def _open_derive_dialog(self, dialog) -> None:  # noqa: ANN001 - DeriveDialog
        previous = getattr(self, "_derive_dialog", None)
        if previous is not None:
            previous.close()
        self._derive_dialog = dialog
        dialog.setModal(False)

        def run() -> None:
            mode, name, options = dialog.result_spec()
            self._run_derive([(source, name) for source in dialog.sources], mode, options)

        def busy(_title: str) -> None:
            dialog.set_busy(True)

        def idle(*_args: object) -> None:
            dialog.set_busy(False)

        def finished(_code: int) -> None:
            self.jobs.started.disconnect(busy)
            self.jobs.done.disconnect(idle)
            if getattr(self, "_derive_dialog", None) is dialog:
                self._derive_dialog = None
            dialog.deleteLater()

        dialog.apply_requested.connect(run)
        dialog.accepted.connect(run)
        self.jobs.started.connect(busy)
        self.jobs.done.connect(idle)
        dialog.finished.connect(finished)
        dialog.show()

    def _run_derive(self, jobs: list[tuple[str, str | None]], mode: str,
                    options: dict) -> None:
        project = self.project
        assert project is not None
        title = "Retarget" if len(jobs) == 1 else f"Retarget {len(jobs)} assets"
        self._start_task(title, "derive", jobs=[(s, n) for s, n in jobs], mode=mode,
                         options=dict(options))

    # -- builds ------------------------------------------------------------
    def new_build(self) -> None:
        if self.project is None:
            return
        dialog = NewBuildDialog(self.project, self)
        if dialog.exec() == NewBuildDialog.DialogCode.Accepted and dialog.build:
            self.explorer.show_project(self.project)
            self.explorer.select("build", dialog.build.name)

    def _select_build(self, name: str) -> None:
        self.right.setCurrentWidget(self.build_panel)
        self.build_panel.show_build(self.project, name)

    def run_build(self, name: str, *, then_compile: bool = False) -> None:
        project = self.project
        if project is None or not name or self.jobs.busy:
            if project is not None and not name:
                self.statusBar().showMessage("select a build in the Explorer", 4000)
            return
        if then_compile and not self._studiomdl_ready():
            return

        self._pending_build = name
        title = f"Build + compile {name}" if then_compile else f"Build {name}"
        self._start_task(title, "build", name=name, then_compile=then_compile)

    def plan_build(self, name: str) -> None:
        project = self.project
        if project is None or not name or self.jobs.busy:
            return
        if project.builds[name].kind not in project.PLANNABLE:
            self.statusBar().showMessage("only merge-v builds can be planned", 5000)
            return
        self._pending_build = name
        self._pending_tab = "plan"
        self._start_task(f"Plan {name}", "plan", name=name)

    def deploy_build(self, name: str) -> None:
        """Copy the build's server files (models, manifest, sounds) into the
        game folder, after confirming what gets overwritten."""
        project = self.project
        if project is None or not name or self.jobs.busy:
            return
        if not project.settings.game_dir:
            QMessageBox.information(self, "Deploy",
                                    "Set the game folder in Project ▸ Settings first.")
            return
        try:
            pairs = project.deploy_pairs(name)
        except ProjectError as exc:
            QMessageBox.information(self, "Deploy", str(exc))
            return
        game = Path(project.settings.game_dir)
        lines = []
        for _source, destination in pairs:
            try:
                shown = destination.relative_to(game).as_posix()
            except ValueError:
                shown = str(destination)
            lines.append(f"{shown}{'   (replaces)' if destination.exists() else ''}")
        answer = QMessageBox.question(
            self, "Deploy", f"Copy {len(pairs)} file(s) into {game}?\n\n"
            + "\n".join(lines[:20]) + ("\n…" if len(lines) > 20 else ""))
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._pending_build = name
        self._start_task(f"Deploy {name}", "deploy", name=name)

    def _reveal_asset(self, name: str) -> None:
        """Select an asset in the Explorer (clearing a filter that hides it)."""
        if self.project is None or name not in self.project.assets:
            return
        if not self.explorer.select("asset", name):
            self.explorer_panel.search.clear()
            self.explorer_panel.status_box.setCurrentIndex(0)
            self.explorer.select("asset", name)
        self._select_asset(name)

    def _studiomdl_ready(self) -> bool:
        if self.project is not None and self.project.settings.studiomdl:
            return True
        QMessageBox.information(self, "Compile",
                                "Set the studiomdl path in Project ▸ Settings first.")
        return False

    def compile_build(self, name: str) -> None:
        project = self.project
        if project is None or not name or self.jobs.busy:
            if project is not None and not name:
                self.statusBar().showMessage("select a build in the Explorer", 4000)
            return
        if not self._studiomdl_ready():
            return
        record = load_record(project.build_dir(name) / "last_run.json")
        if record is None or not record.get("outputs"):
            why = ("has not been run yet" if record is None
                   else "emitted no QC on its last run")
            answer = QMessageBox.question(
                self, "Compile", f"Build {name} {why}, so there is nothing to compile.\n\n"
                "Run it now and then compile?")
            if answer == QMessageBox.StandardButton.Yes:
                self.run_build(name, then_compile=True)
            return
        self._pending_build = name
        self._start_task(f"Compile {name}", "compile", name=name)

    def delete_build(self, name: str) -> None:
        if self.project is None or not name or self.jobs.busy:
            return
        answer = QMessageBox.question(self, "Delete build",
                                      f"Delete build {name} and its outputs?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.project.remove_build(name)
        self.explorer.show_project(self.project)
        self.right.setCurrentWidget(self.inspector)

    def preview_output(self, qc_path: str) -> None:
        directory = Path(qc_path).parent
        try:
            scene = build_scene(directory)
        except Exception as exc:  # noqa: BLE001 - shown, not fatal
            self.log.append_line(f"preview failed: {exc}")
            return
        self._shown_asset = ""
        self.viewport.set_compare(None)
        self.viewport.set_scene(scene)
        self.log.append_line(f"preview {Path(qc_path).name}: {len(scene.batches)} batches, "
                             f"{len(scene.sequences)} sequences")

    def preview_body(self, qc_path: str, body: int, sequence: int) -> None:
        self.preview_output(qc_path)
        self.viewport.show_body(body, sequence if sequence >= 0 else None)
        self.log.append_line(f"  pev_body {body} -> {self.viewport.viewport.state.bodygroups}")

    def open_output(self, path: str) -> None:
        target = Path(path)
        if target.suffix.lower() == ".mdl":
            if not target.exists():
                self.statusBar().showMessage("not compiled yet (Build ▸ Compile)", 5000)
                return
            viewer = self.project.settings.hlam if self.project is not None else None
            if viewer:
                import subprocess
                import sys
                if sys.platform == "darwin" and viewer.endswith(".app"):
                    subprocess.Popen(["open", "-a", viewer, str(target)])
                else:
                    subprocess.Popen([viewer, str(target)])
                return
        if not target.exists():
            self.statusBar().showMessage("not there yet: run and compile the build", 5000)
            return
        self._open_path(target)

    def _asset_scene(self, name: str) -> tuple[ModelInfo, ModelScene] | None:
        """Parsed info + scene of an asset (cached); None if it cannot be read."""
        assert self.project is not None
        info = self._info_cache.get(name)
        scene = self._scene_cache.get(name)
        if info is None or scene is None:
            directory = self.project.asset_dir(name)
            try:
                model = load_model(directory, require_anims=False)
                info = read_model_info(directory, model)
                scene = build_scene(directory, model)
            except Exception as exc:  # noqa: BLE001 - a broken asset must not kill the UI
                self.log.append_line(f"{name}: cannot read model ({exc})")
                return None
            self._info_cache[name] = info
            self._scene_cache[name] = scene
        return info, scene

    def _select_asset(self, name: str) -> None:
        self.right.setCurrentWidget(self.inspector)
        if self.project is None or not name:
            self.inspector.show_asset(self.project, None)
            self._shown_asset = ""
            self.viewport.set_compare(None)
            self.viewport.set_scene(None)
            return
        if name not in self._scene_cache:
            self.inspector.show_asset(self.project, None, name)
        loaded = self._asset_scene(name)
        if loaded is None:
            self._shown_asset = ""
            self.viewport.set_compare(None)
            self.viewport.set_scene(None)
            return
        info, scene = loaded
        asset = self.project.assets[name]
        source = asset.derived["from"] if asset.derived else None
        # the same model again (re-run, edit) or its own new version: keep the view
        keep = bool(self._shown_asset) and self._shown_asset in (name, source)
        if self.inspector.qc_page.dirty and self._shown_asset not in ("", name):
            self.log.append_line(f"{self._shown_asset}: unsaved QC edits discarded")
        self._shown_asset = name
        self.inspector.show_asset(self.project, info, name)
        self.inspector.skins_page.set_skins(scene.skins)
        try:
            from valve_qc_merger.project.qc_edit import qc_file
            self.inspector.qc_page.set_text(
                qc_file(self.project.asset_dir(name)).read_text(encoding="latin-1"))
        except (OSError, ValueError):
            self.inspector.qc_page.set_text(None)
        self.inspector.show_status(self.explorer.statuses.get(name))
        pair = getattr(self, "_compare_pair", None)
        if pair is not None and pair[0] == name and pair[1] in self.project.assets:
            self.viewport.set_compare(pair[1], "other")
        else:
            self._compare_pair = None
            self.viewport.set_compare(source if source in self.project.assets else None)
        self._fov_kind = asset.kind
        self.viewport.set_fov(self.fov_for(asset.kind))
        self.viewport.set_view_model(asset.kind in VIEW_MODEL_KINDS, self.right_hand())
        self.viewport.set_scene(scene, keep_view=keep)
        self._set_sound_events(name, scene)
        self._log_renderer(name)

    def _set_sound_events(self, name: str, scene) -> None:  # noqa: ANN001 - ModelScene
        """The asset's sound events (5004 ...) on the timeline, played with
        the animation; files come from the library, then the game folder."""
        from valve_qc_merger.project import sounds
        from valve_qc_merger.project.qc_edit import qc_file
        project = self.project
        try:
            text = qc_file(project.asset_dir(name)).read_text(encoding="latin-1")
        except (OSError, ValueError):
            self.viewport.set_sound_events({})
            return
        by_name = sounds.sequence_sounds(text)
        index = {seq.name: i for i, seq in enumerate(scene.sequences)}
        events = {index[seq]: found for seq, found in by_name.items() if seq in index}
        cache: dict[str, Path | None] = {}

        def resolver(sound: str) -> Path | None:
            if sound not in cache:
                cache[sound] = sounds.resolve(project, sound)
            return cache[sound]
        self.viewport.set_sound_events(events, resolver)

    def _log_renderer(self, name: str) -> None:
        """What the viewport could not do (textures it could not upload) and,
        once, which OpenGL it runs on — the first thing to know on a machine
        where models draw untextured."""
        renderer = self.viewport.viewport.renderer
        if renderer.gl_info and not getattr(self, "_gl_logged", False):
            self._gl_logged = True
            self.log.append_line(f"OpenGL: {renderer.gl_info}")
        for problem in renderer.warnings:
            self.log.append_line(f"warn: {name}: {problem}")

    def right_hand(self) -> bool:
        return str(self.settings.value("viewport/right_hand", "false")).lower() == "true"

    def _right_hand_toggled(self, on: bool) -> None:
        self.settings.setValue("viewport/right_hand", "true" if on else "false")

    def fov_for(self, kind: str) -> float:
        """First-person FOV for an asset kind: the last one set for it, else
        74 (zombie hands: 84)."""
        from valve_qc_merger.studio.renderer import FP_FOV, FP_FOV_BY_KIND
        stored = self.settings.value(f"viewport/fp_fov/{kind}")
        try:
            return float(stored) if stored is not None else FP_FOV_BY_KIND.get(kind, FP_FOV)
        except (TypeError, ValueError):
            return FP_FOV_BY_KIND.get(kind, FP_FOV)

    def _fov_edited(self, fov: float) -> None:
        kind = getattr(self, "_fov_kind", "")
        if kind:
            self.settings.setValue(f"viewport/fp_fov/{kind}", fov)

    def _compare(self, show_source: bool) -> None:
        """Before/after: swap the viewport to the shown asset's source and back,
        same camera, sequence and frame."""
        project, name = self.project, self._shown_asset
        if project is None or not name or name not in project.assets:
            return
        derived = project.assets[name].derived
        pair = getattr(self, "_compare_pair", None)
        other = pair[1] if pair is not None and pair[0] == name else (
            derived["from"] if derived else None)
        target = other if show_source and other else name
        loaded = self._asset_scene(target)
        if loaded is not None:
            self.viewport.set_scene(loaded[1], keep_view=True)

    def _reveal(self, name: str) -> None:
        if self.project is not None:
            self._open_path(self.project.asset_dir(name))

    def _open_path(self, path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def edit_settings(self) -> None:
        if self.project is None:
            return
        dialog = SettingsDialog(self.project.settings, self)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            dialog.apply_to(self.project.settings)
            self.project.save()

    # -- jobs --------------------------------------------------------------
    def _job_started(self, title: str) -> None:
        self._job_mark = (time.monotonic(), self.log.warnings, self.log.errors)
        self.log.append_line(f"── {title}")
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self.cancel_button.setVisible(True)
        self.statusBar().showMessage(title)
        self._update_actions()

    def _job_progress(self, done: int, total: int, label: str) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        self.statusBar().showMessage(label)

    def _start_task(self, title: str, task: str, **kwargs: object) -> bool:
        """Run a heavy task of :mod:`.tasks` in a process of its own (the
        window keeps painting); the project is saved first and re-read after."""
        if self.project is None:
            return False
        self.project.save()
        return self.jobs.start_process(title, task, root=str(self.project.root), **kwargs)

    def _reload_project(self) -> None:
        """Re-read the project a job process changed on disk; panels holding
        the old object get the new one."""
        if self.project is None:
            return
        try:
            fresh = Project.open(self.project.root)
        except (ProjectError, OSError, ValueError) as exc:
            self.log.append_line(f"warn: could not re-read the project: {exc}")
            return
        self.project = fresh
        window = getattr(self, "server_window", None)
        if window is not None:
            window.project = fresh
            for panel in vars(window).values():
                if hasattr(panel, "project") and panel is not window:
                    panel.project = fresh

    def _flush_log(self) -> None:
        if self._log_buffer:
            lines, self._log_buffer[:] = list(self._log_buffer), []
            self.log.append_lines(lines)

    def graphics_report(self) -> str:
        """Help ▸ Graphics report: what the viewport's OpenGL does, to copy
        into a bug report (the grid / bones really reaching the screen)."""
        from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QPlainTextEdit

        from valve_qc_merger.studio import dialog_kit as kit
        self.center.setCurrentWidget(self.viewport)
        text = self.viewport.viewport.graphics_report()
        self.log.append_line("graphics report:\n" + text)
        dialog = QDialog(self)
        dialog.setWindowTitle("Graphics report")
        layout = kit.dialog_layout(dialog)
        layout.addWidget(kit.header("Graphics report", "Copy this into a bug report: it "
                                    "says which OpenGL the viewport got and what reached "
                                    "the screen (0 grid pixels: the grid is not drawn)."))
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        layout.addWidget(view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = buttons.addButton("Copy", QDialogButtonBox.ButtonRole.ActionRole)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(text))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(640, 360)
        self._graphics_dialog = dialog
        dialog.show()
        return text

    def _job_done(self, title: str, ok: bool, payload: object) -> None:
        self._flush_log()
        if self.jobs.process_job:
            self._reload_project()
            if isinstance(payload, dict) and payload.get("made"):
                self._pending_asset = payload["made"][-1]  # a retarget's last result
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        started, warnings, errors = self._job_mark
        seconds = time.monotonic() - started if started else 0.0
        if ok:
            self.statusBar().showMessage(f"{title}: done", 5000)
        else:
            self.log.append_line(f"{title} failed: {payload}")
            self.statusBar().showMessage(f"{title}: {payload}", 8000)
        self._report_job(title, ok, str(payload) if not ok else "", seconds,
                         self.log.warnings - warnings, self.log.errors - errors)
        folder = getattr(self, "_open_after_job", None)
        self._open_after_job = None
        if ok and folder is not None and Path(folder).is_dir():
            self._open_path(Path(folder))
        window = getattr(self, "server_window", None)
        if window is not None and window.isVisible():
            window.refresh()  # a build ran: new outputs, new budget
        if self.project is not None:
            self._info_cache.clear()
            self._scene_cache.clear()
            self.explorer.show_project(self.project)
            asset = getattr(self, "_pending_asset", "")
            if asset and asset in self.project.assets:
                self.explorer.select("asset", asset)
                self._select_asset(asset)
                self.right.setCurrentWidget(self.inspector)
            self._pending_asset = ""
            pending = getattr(self, "_pending_build", "")
            if pending and pending in self.project.builds:
                self.explorer.select("build", pending)
                self.build_panel.show_build(self.project, pending)
                self.right.setCurrentWidget(self.build_panel)
                tab = getattr(self, "_pending_tab", "")
                self.build_panel.setCurrentIndex(self.build_panel.plan_tab if tab == "plan"
                                                 else 1)  # Results
            self._pending_build = ""
            self._pending_tab = ""
        self._update_actions()

    def _report_job(self, title: str, ok: bool, why: str, seconds: float,
                    warnings: int, errors: int) -> None:
        """Toast what just finished; a failure (or error lines) opens the log."""
        took = f"in {seconds:.1f} s" if seconds >= 0.05 else ""
        if not ok:
            self.toast.show_message("error", f"{title} failed", why[:240])
            self.show_log()
        elif errors:
            self.toast.show_message("error", f"{title}: {errors} error(s)",
                                    f"finished {took}; see the log for what failed")
            self.show_log()
        elif warnings:
            self.toast.show_message("warning", f"{title}: done with {warnings} warning(s)",
                                    took)
        else:
            self.toast.show_message("success", f"{title}: done", took)

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        if self.jobs.busy:
            answer = QMessageBox.question(self, "Quit", "A job is running. Cancel it and quit?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.jobs.cancel()
            self.jobs.wait(30_000)
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/state", self.saveState())
        self.viewport.viewport.release_gl()
        event.accept()


__all__ = ["MainWindow"]
