"""QC editing in the studio: the sequence dialog (properties + events), the
raw QC page with highlighting, and the skins page."""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.project.qc_edit import (
    EVENT_NAMES,
    LABEL_LIMIT,
    SequenceBlock,
    SequenceEvent,
)


# --------------------------------------------------------------------------- #
# Sequence dialog
# --------------------------------------------------------------------------- #
class SequenceDialog(QDialog):
    """Name, fps, loop, activity and the event list of one sequence."""

    COLUMNS = ["Frame", "Event", "Option", "Meaning"]

    def __init__(self, block: SequenceBlock, frames: int | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Sequence — {block.name}")
        self.block = block
        self.frames = frames
        self.name_edit = QLineEdit(block.name)
        self.name_edit.setMaxLength(LABEL_LIMIT)
        self.fps_box = QCheckBox("fps")
        self.fps_box.setChecked(block.fps is not None)
        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setRange(0.1, 1000.0)
        self.fps_spin.setDecimals(2)
        self.fps_spin.setValue(block.fps or 30.0)
        self.fps_spin.setEnabled(block.fps is not None)
        self.fps_box.toggled.connect(self.fps_spin.setEnabled)
        fps_row = QWidget()
        fps_layout = QHBoxLayout(fps_row)
        fps_layout.setContentsMargins(0, 0, 0, 0)
        fps_layout.addWidget(self.fps_box)
        fps_layout.addWidget(self.fps_spin, 1)
        self.loop_box = QCheckBox("loop (idles wrap instead of stopping)")
        self.loop_box.setChecked(block.loop)
        self.activity_edit = QLineEdit(block.activity or "")
        self.activity_edit.setPlaceholderText("e.g. ACT_VM_IDLE 1 (view models: unused)")
        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Speed", fps_row)
        form.addRow("", self.loop_box)
        form.addRow("Activity", self.activity_edit)
        animation = ", ".join(block.paths) or "—"
        extra = f" · options kept: {block.other}" if block.other else ""
        info = QLabel(f"Animation: {animation}{extra}"
                      + (f" · {frames} frames" if frames else ""))
        info.setWordWrap(True)
        info.setProperty("role", "hint")

        self.events = QTableWidget(0, len(self.COLUMNS))
        self.events.setHorizontalHeaderLabels(self.COLUMNS)
        self.events.verticalHeader().setVisible(False)
        self.events.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        self.events.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.events.itemChanged.connect(self._event_changed)
        for event in block.events:
            self._add_row(event)
        add = QPushButton("Add event")
        add.clicked.connect(lambda: self._add_row(SequenceEvent(5004, 0, "")))
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_rows)
        buttons_row = QHBoxLayout()
        buttons_row.addWidget(add)
        buttons_row.addWidget(remove)
        buttons_row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(info)
        layout.addWidget(QLabel("Events (5001/5011/5021/5031 = muzzle flash on attachment "
                                "0-3, 5004 = sound; option = the event's text)"))
        layout.addWidget(self.events, 1)
        layout.addLayout(buttons_row)
        layout.addWidget(buttons)
        self.resize(620, 460)

    def _add_row(self, event: SequenceEvent) -> None:
        self.events.blockSignals(True)
        row = self.events.rowCount()
        self.events.insertRow(row)
        for column, value in enumerate((event.frame, event.event, event.option,
                                        EVENT_NAMES.get(event.event, ""))):
            item = QTableWidgetItem(str(value))
            if column == 3:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.events.setItem(row, column, item)
        self.events.blockSignals(False)

    def _event_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 1:
            return
        meaning = self.events.item(item.row(), 3)
        if meaning is not None:
            try:
                meaning.setText(EVENT_NAMES.get(int(item.text()), ""))
            except ValueError:
                meaning.setText("")

    def _remove_rows(self) -> None:
        for row in sorted({i.row() for i in self.events.selectedIndexes()}, reverse=True):
            self.events.removeRow(row)

    def values(self) -> dict:
        """Edited fields for :func:`set_sequence` (raises ValueError)."""
        events = []
        for row in range(self.events.rowCount()):
            frame = int(self.events.item(row, 0).text())
            event = int(self.events.item(row, 1).text())
            option = self.events.item(row, 2).text().strip()
            if self.frames is not None and not 0 <= frame < max(self.frames, 1):
                raise ValueError(f"event frame {frame} is outside 0..{self.frames - 1}")
            events.append(SequenceEvent(event, frame, option))
        return {
            "name": self.name_edit.text().strip(),
            "fps": self.fps_spin.value() if self.fps_box.isChecked() else None,
            "loop": self.loop_box.isChecked(),
            "activity": self.activity_edit.text().strip() or None,
            "events": events,
        }

    def _accept(self) -> None:
        try:
            self.values()
        except ValueError as exc:
            QMessageBox.warning(self, "Sequence", f"Check the events: {exc}")
            return
        self.accept()


# --------------------------------------------------------------------------- #
# Raw QC page
# --------------------------------------------------------------------------- #
class QcHighlighter(QSyntaxHighlighter):
    """$commands, strings, numbers, ACT_ activities, comments."""

    def __init__(self, document) -> None:  # noqa: ANN001 - QTextDocument
        super().__init__(document)

        def fmt(color: str, bold: bool = False) -> QTextCharFormat:
            out = QTextCharFormat()
            out.setForeground(QColor(color))
            if bold:
                out.setFontWeight(QFont.Weight.Bold)
            return out

        self.rules = [
            (re.compile(r"-?\b\d+(?:\.\d+)?\b"), fmt("#e0b26a")),
            (re.compile(r"\bACT_[A-Za-z0-9_]+\b"), fmt("#c49bff")),
            (re.compile(r"\b(?:fps|loop|event|studio|blank)\b"), fmt("#5fc4e0")),
            (re.compile(r"\$[A-Za-z]+"), fmt("#7ba3ff", bold=True)),
            (re.compile(r'"[^"]*"'), fmt("#7fd1a3")),
            (re.compile(r"//.*$"), fmt("#6b7282")),
        ]

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt override
        for pattern, style in self.rules:
            for match in pattern.finditer(text):
                self.setFormat(match.start(), match.end() - match.start(), style)


class QcPage(QWidget):
    """The asset's QC as text: edit, Save (validated: the model must still
    load, else nothing is written) or Revert."""

    save_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.highlighter = QcHighlighter(self.editor.document())
        self.save_button = QPushButton("Save QC")
        self.revert_button = QPushButton("Revert")
        self.status = QLabel("")
        self.status.setProperty("role", "hint")
        self.save_button.clicked.connect(lambda: self.save_requested.emit(
            self.editor.toPlainText()))
        self.revert_button.clicked.connect(self._revert)
        self.editor.textChanged.connect(self._changed)
        row = QHBoxLayout()
        row.addWidget(self.status, 1)
        row.addWidget(self.revert_button)
        row.addWidget(self.save_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.editor, 1)
        layout.addLayout(row)
        self._original = ""
        self.set_text(None)

    def set_text(self, text: str | None) -> None:
        self._original = text or ""
        self.editor.blockSignals(True)
        self.editor.setPlainText(self._original)
        self.editor.blockSignals(False)
        self.editor.setEnabled(text is not None)
        self._changed()

    def _revert(self) -> None:
        self.set_text(self._original if self.editor.isEnabled() else None)

    @property
    def dirty(self) -> bool:
        return self.editor.isEnabled() and self.editor.toPlainText() != self._original

    def _changed(self) -> None:
        self.save_button.setEnabled(self.dirty)
        self.revert_button.setEnabled(self.dirty)
        self.status.setText("modified — Save checks that the model still loads"
                            if self.dirty else "")


# --------------------------------------------------------------------------- #
# Skins page
# --------------------------------------------------------------------------- #
class SkinsPage(QWidget):
    """``$texturegroup`` rows; picking one previews it in the viewport."""

    skin_selected = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Skin", "Textures"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.currentCellChanged.connect(
            lambda row, *_a: row >= 0 and self.skin_selected.emit(row))
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setProperty("role", "hint")
        layout = QVBoxLayout(self)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.note)
        self.set_skins([])

    def set_skins(self, skins: list[list[str]]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(skins))
        for row, textures in enumerate(skins):
            self.table.setItem(row, 0, QTableWidgetItem("base" if row == 0 else str(row)))
            self.table.setItem(row, 1, QTableWidgetItem(", ".join(textures)))
        if skins:
            self.table.selectRow(0)
        self.table.blockSignals(False)
        self.table.setVisible(len(skins) > 1)  # one skin: the note says it all
        self.note.setText(
            "No $texturegroup: one skin." if len(skins) < 2 else
            "merge-v turns each extra skin into its own weapon entry <model>_skin<k> "
            "(the server can set a view model's body, not its skin).")


__all__ = ["QcHighlighter", "QcPage", "SequenceDialog", "SkinsPage"]
