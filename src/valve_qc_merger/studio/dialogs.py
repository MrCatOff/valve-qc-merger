"""Small dialogs: new project, project settings."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QToolButton,
    QWidget,
)

from valve_qc_merger.project import Settings
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio.icons import icon


def _path_row(edit: QLineEdit, pick) -> QWidget:  # noqa: ANN001 - callable
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(edit, 1)
    button = QToolButton()
    button.setIcon(icon("folder-open"))
    button.setToolTip("Browse…")
    button.setIconSize(QSize(16, 16))
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
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header("New project", "One folder holds a pack's models, "
                                    "builds and settings."))
        form = kit.form()
        form.addRow("Name", self.name_edit)
        form.addRow("Location", _path_row(self.folder_edit, self._pick))
        layout.addLayout(form)
        self.where = kit.hint("")
        layout.addWidget(self.where)
        for edit in (self.name_edit, self.folder_edit):
            edit.textChanged.connect(self._preview)
        self._preview()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addSpacing(6)
        layout.addWidget(buttons)
        self.resize(540, self.sizeHint().height())

    def _preview(self) -> None:
        root, _name = self.target()
        self.where.setText(f"Creates {root}" + ("  (already exists)" if root.exists() else ""))

    def _pick(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Location", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)

    def target(self) -> tuple[Path, str]:
        name = self.name_edit.text().strip() or "MyPack"
        return Path(self.folder_edit.text()) / name, name


class SettingsDialog(QDialog):
    """Project tools: studiomdl (compile), HLAM (preview), the game (deploy)."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Project settings")
        self.studiomdl_edit = QLineEdit(settings.studiomdl or "")
        self.studiomdl_edit.setPlaceholderText("path to studiomdl(.exe)")
        self.hlam_edit = QLineEdit(settings.hlam or "")
        self.hlam_edit.setPlaceholderText("HLAM / HLMV (optional)")
        self.game_edit = QLineEdit(settings.game_dir or "")
        self.game_edit.setPlaceholderText("e.g. …/Half-Life/cstrike")
        self.deploy_box = QCheckBox("Deploy to the game after every successful compile")
        self.deploy_box.setChecked(settings.deploy_after_compile)
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header("Project settings", "The tools this project uses and "
                                    "where its models go in the game."))
        layout.addWidget(kit.section("Tools"))
        tools = kit.form()
        tools.addRow("studiomdl", _path_row(self.studiomdl_edit,
                                            lambda: self._pick(self.studiomdl_edit)))
        tools.addRow("Model viewer", _path_row(self.hlam_edit,
                                               lambda: self._pick(self.hlam_edit)))
        layout.addLayout(tools)
        layout.addWidget(kit.hint("studiomdl compiles builds (Compile, Run and compile); "
                                  "the model viewer opens compiled models."))
        layout.addWidget(kit.section("Game"))
        game = kit.form()
        game.addRow("Game folder", _path_row(self.game_edit, self._pick_game))
        layout.addLayout(game)
        layout.addWidget(self.deploy_box)
        layout.addWidget(kit.hint("Deploy copies compiled models and their manifest into "
                                  "this mod folder (each build can pick a subfolder)."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addSpacing(6)
        layout.addWidget(buttons)
        self.resize(580, self.sizeHint().height())

    def _pick(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose program", edit.text())
        if path:
            edit.setText(path)

    def _pick_game(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Game (mod) folder",
                                                  self.game_edit.text())
        if folder:
            self.game_edit.setText(folder)

    def apply_to(self, settings: Settings) -> None:
        settings.studiomdl = self.studiomdl_edit.text().strip() or None
        settings.hlam = self.hlam_edit.text().strip() or None
        settings.game_dir = self.game_edit.text().strip() or None
        settings.deploy_after_compile = self.deploy_box.isChecked()


__all__ = ["NewProjectDialog", "SettingsDialog"]
