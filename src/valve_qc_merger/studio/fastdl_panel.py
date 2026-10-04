"""Server ▸ FastDL: is every file of a folder on the download mirror?"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.project import Project
from valve_qc_merger.server import fastdl
from valve_qc_merger.server.cfg import CfgFile
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import icon

GLYPH = {"missing": "✕", "size": "▲", "error": "▲", "ok": "✓"}
TOKEN = {"missing": "danger", "size": "warning", "error": "warning", "ok": "success"}


def _cell(text: object, tip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(str(text))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if tip:
        item.setToolTip(tip)
    return item


def download_url(game_dir: str | None) -> str:
    """``sv_downloadurl`` of the game folder's server.cfg (or game.cfg)."""
    if not game_dir:
        return ""
    for name in ("server.cfg", "game.cfg"):
        value = CfgFile.load(Path(game_dir) / name).get("sv_downloadurl")
        if value:
            return value
    return ""


class FastDlPanel(QWidget):
    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.results: list[fastdl.Result] = []
        self._stop = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 12, 4, 4)
        layout.setSpacing(8)
        layout.addWidget(kit.hint(
            "Asks the download mirror (sv_downloadurl) for every file of a folder — your "
            "exported package's cstrike/, or the game folder — and compares sizes. A missing "
            "file falls back to the slow in-game download; another size means a stale copy. "
            "Paths are asked as they are: a Linux web server is case-sensitive."))
        form = kit.form()
        self.url_edit = QLineEdit(download_url(project.settings.game_dir))
        self.url_edit.setPlaceholderText("http://fastdl.example.com/cstrike/")
        self.folder_edit = QLineEdit(project.settings.game_dir or "")
        self.folder_edit.setPlaceholderText("…/cstrike (what the mirror should hold)")
        folder_row = QWidget()
        row = QHBoxLayout(folder_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.folder_edit, 1)
        pick = QPushButton(icon("folder-open"), "")
        pick.setAutoDefault(False)
        pick.clicked.connect(self._pick)
        row.addWidget(pick)
        form.addRow("Mirror URL", self.url_edit)
        form.addRow("Local folder", folder_row)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.summary_label = QLabel("Not checked yet.")
        self.problems_box = QCheckBox("Problems only")
        self.problems_box.setChecked(True)
        self.problems_box.toggled.connect(lambda _on: self._fill())
        copy = QPushButton(icon("copy"), "Copy report")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(
            fastdl.report(self.url_edit.text().strip(), self.results)))
        self.stop_button = QPushButton(icon("square"), "Stop")
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self._request_stop)
        self.check_button = QPushButton(icon("scan", theme.TOKENS["on_accent"]), "Check")
        theme.set_primary(self.check_button)
        self.check_button.clicked.connect(lambda: self.run())
        for button in (copy, self.stop_button, self.check_button):
            button.setAutoDefault(False)
        actions.addWidget(self.summary_label, 1)
        actions.addWidget(self.problems_box)
        actions.addWidget(copy)
        actions.addWidget(self.stop_button)
        actions.addWidget(self.check_button)
        layout.addLayout(actions)
        self.progress = QProgressBar()
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", "File", "Here", "Mirror", "Problem"])
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in (0, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 280)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)

    def _pick(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Local folder", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)

    def _request_stop(self) -> None:
        self._stop = True

    def run(self, *, workers: int = 8, timeout: float = 10.0) -> list[fastdl.Result]:
        base = self.url_edit.text().strip()
        folder = Path(self.folder_edit.text().strip())
        if not base.lower().startswith(("http://", "https://")):
            self.summary_label.setText("Enter the mirror's http:// address first.")
            return []
        if not folder.is_dir():
            self.summary_label.setText("Choose the local folder the mirror should hold.")
            return []
        files = fastdl.local_files(folder)
        if not files:
            self.summary_label.setText("The folder holds no game files.")
            return []
        self._stop = False
        self.check_button.setEnabled(False)
        self.stop_button.setVisible(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, len(files))

        def progress(done: int, total: int, path: str) -> bool:
            self.progress.setValue(done)
            self.summary_label.setText(f"{done} / {total}  {path}")
            QApplication.processEvents()
            return self._stop

        try:
            self.results = fastdl.check(base, files, workers=workers, timeout=timeout,
                                        progress=progress)
        finally:
            self.check_button.setEnabled(True)
            self.stop_button.setVisible(False)
            self.progress.setVisible(False)
        counts = fastdl.summary(self.results)
        stopped = "  (stopped)" if self._stop else ""
        self.summary_label.setText(
            "  ·  ".join(f"{GLYPH[s]} {counts[s]} {s}" for s in fastdl.STATES if counts[s])
            + stopped)
        self._fill()
        return self.results

    def shown(self) -> list[fastdl.Result]:
        only = self.problems_box.isChecked()
        return [r for r in self.results if not only or r.state != "ok"]

    def _fill(self) -> None:
        shown = self.shown()
        self.table.setRowCount(len(shown))
        for row, result in enumerate(shown):
            glyph = _cell(GLYPH[result.state], result.state)
            glyph.setForeground(theme.color(TOKEN[result.state]))
            self.table.setItem(row, 0, glyph)
            self.table.setItem(row, 1, _cell(result.path, result.path))
            self.table.setItem(row, 2, _cell(result.local_size))
            self.table.setItem(row, 3, _cell("" if result.remote_size is None
                                             else result.remote_size))
            self.table.setItem(row, 4, _cell(result.detail, result.detail))


__all__ = ["FastDlPanel", "download_url"]
