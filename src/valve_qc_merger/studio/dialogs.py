"""Small dialogs: new project, project settings."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QWidget,
)

from valve_qc_merger.project import Settings


def _path_row(edit: QLineEdit, pick) -> QWidget:  # noqa: ANN001 - callable
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(edit, 1)
    button = QPushButton("Browse…")
    button.clicked.connect(pick)
    layout.addWidget(button)
    return row


class NewProjectDialog(QDialog):
    """Name + parent folder; the project lands in <folder>/<name>."""

    def __init__(self, parent: QWidget | None = None, folder: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New project")
        self.name_edit = QLineEdit("MyPack")
        self.folder_edit = QLineEdit(str(folder or Path.home()))
        form = QFormLayout(self)
        form.addRow("Name", self.name_edit)
        form.addRow("Location", _path_row(self.folder_edit, self._pick))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.resize(520, 0)

    def _pick(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Location", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)

    def target(self) -> tuple[Path, str]:
        name = self.name_edit.text().strip() or "MyPack"
        return Path(self.folder_edit.text()) / name, name


class SettingsDialog(QDialog):
    """Project tools: studiomdl (compile) and HLAM (preview)."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Project settings")
        self.studiomdl_edit = QLineEdit(settings.studiomdl or "")
        self.studiomdl_edit.setPlaceholderText("path to studiomdl(.exe)")
        self.hlam_edit = QLineEdit(settings.hlam or "")
        self.hlam_edit.setPlaceholderText("path to HLAM / HLMV (optional)")
        form = QFormLayout(self)
        form.addRow("studiomdl", _path_row(self.studiomdl_edit,
                                           lambda: self._pick(self.studiomdl_edit)))
        form.addRow("Model viewer", _path_row(self.hlam_edit,
                                              lambda: self._pick(self.hlam_edit)))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.resize(560, 0)

    def _pick(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose program", edit.text())
        if path:
            edit.setText(path)

    def apply_to(self, settings: Settings) -> None:
        settings.studiomdl = self.studiomdl_edit.text().strip() or None
        settings.hlam = self.hlam_edit.text().strip() or None


__all__ = ["NewProjectDialog", "SettingsDialog"]
