"""Server ▸ Download: what a new player downloads, and how to make it less."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.project import Project
from valve_qc_merger.server import download
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import icon


def _cell(text: object, tip: str = "", align_right: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(str(text))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if tip:
        item.setToolTip(tip)
    if align_right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


def _kind(path: str) -> str:
    low = path.lower()
    return ("sounds" if low.startswith("sound/") else "sprites" if low.startswith("sprites/")
            else "models" if low.endswith(".mdl") else "other")


class DownloadPanel(QWidget):
    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.files: dict[str, Path] = {}
        self.sizes: dict[str, int] = {}
        self.found: list[download.Suggestion] = []
        self._scanned = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 12, 4, 4)
        layout.setSpacing(8)
        layout.addWidget(kit.hint(
            "What a new player downloads from your package (compiled builds, the sounds "
            "they play, the sprites) — maps add their own, see Maps — and where the bytes "
            "buy nothing. Times are rough: FastDL ~2 MB/s, the engine's own download ~30 "
            "KB/s (sv_rehlds_force_dlmax 1 helps)."))
        self.total_label = QLabel("Not counted yet.")
        self.total_label.setStyleSheet("font-weight: 600;")
        self.kinds_label = kit.hint("")
        layout.addWidget(self.total_label)
        layout.addWidget(self.kinds_label)
        row = QHBoxLayout()
        self.view_box = QComboBox()
        self.view_box.addItem("Ways to save", "suggestions")
        self.view_box.addItem("Largest files", "largest")
        self.view_box.currentIndexChanged.connect(lambda _i: self._fill())
        self.saving_label = QLabel()
        theme.set_role(self.saving_label, "success")
        refresh = QPushButton(icon("refresh-cw"), "Recount")
        refresh.setAutoDefault(False)
        refresh.clicked.connect(self.scan)
        copy = QPushButton(icon("copy"), "Copy report")
        copy.setAutoDefault(False)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.report()))
        row.addWidget(self.view_box)
        row.addWidget(self.saving_label, 1)
        row.addWidget(copy)
        row.addWidget(refresh)
        layout.addLayout(row)
        self.table = QTableWidget(0, 3)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 260)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)

    def showEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        super().showEvent(event)
        if not self._scanned:
            self.scan()

    def scan(self) -> None:
        self._scanned = True
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.files = download.package_files(self.project)
            self.sizes = {p: f.stat().st_size for p, f in self.files.items() if f.is_file()}
            self.found = download.suggestions(self.files)
        finally:
            QApplication.restoreOverrideCursor()
        total = self.total
        if not self.files:
            self.total_label.setText("Nothing to download yet: run and compile a build.")
            self.kinds_label.setText("")
        else:
            self.total_label.setText(
                f"A new player downloads {download.human(total)} ({len(self.files)} files): "
                f"about {download.duration(total, download.FASTDL_BPS)} over FastDL, "
                f"{download.duration(total, download.INGAME_BPS)} without it.")
            kinds: dict[str, int] = {}
            for path, size in self.sizes.items():
                kinds[_kind(path)] = kinds.get(_kind(path), 0) + size
            self.kinds_label.setText("  ·  ".join(
                f"{kind} {download.human(size)}" for kind, size in
                sorted(kinds.items(), key=lambda kv: -kv[1])))
        self._fill()

    @property
    def total(self) -> int:
        return sum(self.sizes.values())

    @property
    def saving(self) -> int:
        return sum(s.saving for s in self.found)

    def _fill(self) -> None:
        if self.view_box.currentData() == "largest":
            self.table.setHorizontalHeaderLabels(["Size", "File", "Share"])
            rows = sorted(self.sizes.items(), key=lambda kv: -kv[1])[:50]
            self.table.setRowCount(len(rows))
            for row, (path, size) in enumerate(rows):
                share = size / self.total * 100 if self.total else 0
                self.table.setItem(row, 0, _cell(download.human(size), align_right=True))
                self.table.setItem(row, 1, _cell(path, path))
                self.table.setItem(row, 2, _cell(f"{share:.1f} %"))
        else:
            self.table.setHorizontalHeaderLabels(["Saves", "File", "Suggestion"])
            self.table.setRowCount(len(self.found))
            for row, found in enumerate(self.found):
                self.table.setItem(row, 0, _cell(f"~{download.human(found.saving)}",
                                                 align_right=True))
                self.table.setItem(row, 1, _cell(found.path, found.path))
                self.table.setItem(row, 2, _cell(found.message, found.message))
        self.saving_label.setText(
            f"could save ~{download.human(self.saving)} "
            f"({self.saving / self.total * 100:.0f} %)" if self.found and self.total else
            ("Nothing obvious to trim." if self.files else ""))

    def report(self) -> str:
        lines = [self.total_label.text(), self.kinds_label.text(), ""]
        lines += [f"~{download.human(s.saving)}  {s.path}: {s.message}" for s in self.found]
        return "\n".join(lines) + "\n"


__all__ = ["DownloadPanel"]
