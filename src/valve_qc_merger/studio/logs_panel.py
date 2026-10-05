"""Server ▸ Logs: what the server's logs say about the resource pack."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
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
from valve_qc_merger.server import logs
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import icon

GLYPH = {"error": "✕", "warning": "▲"}
TOKEN = {"error": "danger", "warning": "warning"}
TAB_TITLES = {"budget": "Budget", "doctor": "Doctor", "unprecache": "Unprecache",
              "entities": "Entities", "maps": "Maps", "download": "Download"}


def _cell(text: object, tip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(str(text))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if tip:
        item.setToolTip(tip)
    return item


class LogsPanel(QWidget):
    def __init__(self, project: Project, goto: Callable[[str], None] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.goto = goto
        self.findings: list[logs.Finding] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 12, 4, 4)
        layout.setSpacing(8)
        layout.addWidget(kit.hint(
            "Reads server logs (logs/, addons/amxmodx/logs/) or a console you copied for "
            "what a resource pack breaks: precache limits, missing or unprecached files, "
            "edicts, memory, overflows, AMXX plugins that fail — grouped, counted, with "
            "what to do and the tab that helps."))
        row = QHBoxLayout()
        scan = QPushButton(icon("folder-search", theme.TOKENS["on_accent"]),
                           "Read the server's logs")
        theme.set_primary(scan)
        scan.clicked.connect(lambda: self.scan_folder())
        pick = QPushButton(icon("file-down"), "Open log files…")
        pick.clicked.connect(self._pick)
        paste = QPushButton(icon("copy"), "Read the clipboard")
        paste.setToolTip("Paste a console (server or client) copied to the clipboard")
        paste.clicked.connect(lambda: self.analyze_text(QApplication.clipboard().text(),
                                                        "clipboard"))
        for button in (scan, pick, paste):
            button.setAutoDefault(False)
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        self.summary = QLabel("Nothing read yet.")
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", "Problem", "File / plugin", "Times",
                                              "Where"])
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in (0, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(2, 220)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.currentCellChanged.connect(lambda *_a: self._show_current())
        layout.addWidget(self.table, 1)
        self.advice = QLabel()
        self.advice.setWordWrap(True)
        self.advice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.advice.setMinimumHeight(60)
        self.advice.setAlignment(Qt.AlignmentFlag.AlignTop)
        bottom = QHBoxLayout()
        self.goto_button = QPushButton(icon("arrow-right"), "")
        self.goto_button.setVisible(False)
        self.goto_button.clicked.connect(self._goto)
        copy = QPushButton(icon("copy"), "Copy report")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(
            logs.report(self.findings)))
        for button in (self.goto_button, copy):
            button.setAutoDefault(False)
        bottom.addWidget(self.advice, 1)
        bottom.addWidget(self.goto_button, 0, Qt.AlignmentFlag.AlignTop)
        bottom.addWidget(copy, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(bottom)

    def scan_folder(self, folder: Path | None = None) -> list[logs.Finding]:
        game = folder or (Path(self.project.settings.game_dir)
                          if self.project.settings.game_dir else None)
        files = logs.log_files(game) if game else []
        if not files:
            self.summary.setText("No .log files in the game folder (logs/, "
                                 "addons/amxmodx/logs/): open some or read the clipboard.")
            self.findings = []
            self._fill()
            return []
        return self.analyze_paths(files)

    def _pick(self) -> None:
        start = self.project.settings.game_dir or str(Path.home())
        files, _ = QFileDialog.getOpenFileNames(self, "Log files", start,
                                                "Logs (*.log *.txt);;All files (*)")
        if files:
            self.analyze_paths([Path(f) for f in files])

    def analyze_paths(self, paths: list[Path]) -> list[logs.Finding]:
        self.findings = logs.analyze_files(paths)
        self._summarise(f"{len(paths)} log file(s)")
        return self.findings

    def analyze_text(self, text: str, source: str = "") -> list[logs.Finding]:
        self.findings = logs.analyze(text, source)
        self._summarise(f"{len(text.splitlines())} line(s) from the {source or 'text'}")
        return self.findings

    def _summarise(self, what: str) -> None:
        errors = sum(1 for f in self.findings if f.rule.severity == "error")
        warnings = len(self.findings) - errors
        self.summary.setText(
            f"{what}: nothing a resource pack explains." if not self.findings else
            f"{what}: {GLYPH['error']} {errors} error kind(s)  ·  "
            f"{GLYPH['warning']} {warnings} warning kind(s)")
        self._fill()

    def _fill(self) -> None:
        self.table.setRowCount(len(self.findings))
        for row, finding in enumerate(self.findings):
            severity = finding.rule.severity
            glyph = _cell(GLYPH[severity], severity)
            glyph.setForeground(theme.color(TOKEN[severity]))
            self.table.setItem(row, 0, glyph)
            self.table.setItem(row, 1, _cell(finding.rule.title, finding.example))
            what = finding.subject or finding.detail
            self.table.setItem(row, 2, _cell(what, what))
            self.table.setItem(row, 3, _cell(finding.count))
            where = (f"{finding.source}:{finding.first_line}" if finding.source
                     else f"line {finding.first_line}")
            self.table.setItem(row, 4, _cell(where))
        if self.findings:
            self.table.selectRow(0)
        self._show_current()

    def current(self) -> logs.Finding | None:
        row = self.table.currentRow()
        return self.findings[row] if 0 <= row < len(self.findings) else None

    def _show_current(self) -> None:
        finding = self.current()
        if finding is None:
            self.advice.setText("")
            self.goto_button.setVisible(False)
            return
        self.advice.setText(f"{finding.rule.advice}\n\n{finding.example}")
        tab = finding.rule.tab
        self.goto_button.setVisible(bool(tab and self.goto))
        self.goto_button.setText(f"Open {TAB_TITLES.get(tab, tab)}")

    def _goto(self) -> None:
        finding = self.current()
        if finding is not None and finding.rule.tab and self.goto is not None:
            self.goto(finding.rule.tab)


__all__ = ["LogsPanel"]
