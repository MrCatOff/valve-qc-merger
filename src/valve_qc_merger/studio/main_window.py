"""The studio main window: Explorer | viewport | Inspector, Log below."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QWidget,
)

from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.project import Project, ProjectError
from valve_qc_merger.services.base import Reporter
from valve_qc_merger.studio.dialogs import NewProjectDialog, SettingsDialog
from valve_qc_merger.studio.jobs import JobRunner
from valve_qc_merger.studio.model_info import ModelInfo, read_model_info
from valve_qc_merger.studio.scene import ModelScene, build_scene
from valve_qc_merger.studio.viewport import ViewportPanel
from valve_qc_merger.studio.widgets import (
    KIND_TITLES,
    Explorer,
    Inspector,
    LogPanel,
    project_title,
)

RECENT_LIMIT = 8


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None) -> None:
        super().__init__()
        self.settings = settings or QSettings("valve-qc-merger", "studio")
        self.project: Project | None = None
        self._info_cache: dict[str, ModelInfo] = {}
        self.jobs = JobRunner(self)
        self.setWindowTitle(project_title(None))
        self.resize(1400, 860)

        self.viewport = ViewportPanel()
        self.setCentralWidget(self.viewport)
        self._scene_cache: dict[str, ModelScene] = {}

        self.explorer = Explorer()
        self.inspector = Inspector()
        self.log = LogPanel()
        self._dock("Explorer", self.explorer, Qt.DockWidgetArea.LeftDockWidgetArea, 300)
        self._dock("Inspector", self.inspector, Qt.DockWidgetArea.RightDockWidgetArea, 460)
        self._dock("Log", self.log, Qt.DockWidgetArea.BottomDockWidgetArea, 180)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setVisible(False)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.jobs.cancel)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.cancel_button)

        self._build_menus()
        self.explorer.asset_selected.connect(self._select_asset)
        self.explorer.remove_requested.connect(self.remove_asset)
        self.explorer.kind_change_requested.connect(self.set_kind)
        self.explorer.reveal_requested.connect(self._reveal)
        self.inspector.kind_changed.connect(self.set_kind)
        self.inspector.notes_changed.connect(self._set_notes)
        self.jobs.started.connect(self._job_started)
        self.jobs.log.connect(self.log.append_line)
        self.jobs.progress.connect(self._job_progress)
        self.jobs.done.connect(self._job_done)
        self._update_actions()

    # -- layout ------------------------------------------------------------
    def _dock(self, title: str, widget: QWidget, area: Qt.DockWidgetArea, size: int) -> None:
        dock = QDockWidget(title, self)
        dock.setObjectName(title)
        dock.setWidget(widget)
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
        project_menu.addSeparator()
        self.act_settings = project_menu.addAction("Settings…", self.edit_settings)
        self.act_reveal = project_menu.addAction("Show project folder",
                                                 lambda: self._open_path(self.project.root))
        self._refresh_recent()

    def _update_actions(self) -> None:
        has = self.project is not None
        idle = not self.jobs.busy
        for action in (self.act_import_mdl, self.act_import_mdl_dir, self.act_import_dec,
                       self.act_settings, self.act_reveal, self.act_close):
            action.setEnabled(has and idle)
        for action in (self.act_new, self.act_open):
            action.setEnabled(idle)
        self.recent_menu.setEnabled(idle)

    # -- project lifecycle -------------------------------------------------
    def new_project(self) -> None:
        dialog = NewProjectDialog(self, Path(self.settings.value("last_folder", str(Path.home()))))
        if dialog.exec() != NewProjectDialog.DialogCode.Accepted:
            return
        root, name = dialog.target()
        try:
            project = Project.create(root, name)
        except (ProjectError, OSError) as exc:
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
        self.project = project
        self._info_cache.clear()
        self._scene_cache.clear()
        self.viewport.set_scene(None)
        self.setWindowTitle(project_title(project))
        self.explorer.show_project(project)
        self.inspector.show_asset(project, None)
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

    def _run_import(self, title: str, sources: list[Path], *, mdl: bool) -> None:
        project = self.project
        assert project is not None

        def work(reporter: Reporter) -> list[str]:
            names: list[str] = []
            for done, source in enumerate(sources):
                reporter.check()
                reporter.progress(done, len(sources), source.name)
                if mdl:
                    added = project.import_mdl(source, reporter=reporter)
                else:
                    added = project.import_decompiled(source)
                names += [a.name for a in added]
                for asset in added:
                    reporter.log(f"  + {asset.name:<28} {KIND_TITLES[asset.kind]}")
            return names

        self.jobs.start(title, work)

    def remove_asset(self, name: str) -> None:
        if self.project is None or self.jobs.busy:
            return
        answer = QMessageBox.question(
            self, "Remove asset",
            f"Remove {name} from the project and delete its folder?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.project.remove_asset(name)
        self._info_cache.pop(name, None)
        self._scene_cache.pop(name, None)
        self.log.append_line(f"removed {name}")
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

    def _select_asset(self, name: str) -> None:
        if self.project is None or not name:
            self.inspector.show_asset(self.project, None)
            self.viewport.set_scene(None)
            return
        info = self._info_cache.get(name)
        scene = self._scene_cache.get(name)
        if info is None or scene is None:
            self.inspector.show_asset(self.project, None, name)
            directory = self.project.asset_dir(name)
            try:
                model = load_model(directory, require_anims=False)
                info = read_model_info(directory, model)
                scene = build_scene(directory, model)
            except Exception as exc:  # noqa: BLE001 - a broken asset must not kill the UI
                self.log.append_line(f"{name}: cannot read model ({exc})")
                self.viewport.set_scene(None)
                return
            self._info_cache[name] = info
            self._scene_cache[name] = scene
        self.inspector.show_asset(self.project, info, name)
        self.viewport.set_scene(scene)

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

    def _job_done(self, title: str, ok: bool, payload: object) -> None:
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        if ok:
            self.statusBar().showMessage(f"{title}: done", 5000)
        else:
            self.log.append_line(f"{title} failed: {payload}")
            self.statusBar().showMessage(f"{title}: {payload}", 8000)
        if self.project is not None:
            self._info_cache.clear()
            self._scene_cache.clear()
            self.explorer.show_project(self.project)
        self._update_actions()

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        if self.jobs.busy:
            answer = QMessageBox.question(self, "Quit", "A job is running. Cancel it and quit?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.jobs.cancel()
            self.jobs.wait(30_000)
        event.accept()


__all__ = ["MainWindow"]
