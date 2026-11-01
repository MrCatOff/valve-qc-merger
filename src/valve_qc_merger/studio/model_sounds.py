"""Asset ▸ Sounds of this model: every sound the weapon's events play, each
with another to play instead — from the library or the game — heard first,
replaced in every sequence at once (no QC editing). The sound pickers here
also serve the sequence dialog's sound events."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QWidget,
)

from valve_qc_merger.project import Project
from valve_qc_merger.project import sounds as library
from valve_qc_merger.studio import audio, theme
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio.icons import icon

GAME_MARK = "  (game)"


def sound_choices(project: Project) -> list[str]:
    """Sounds a picker offers: the library's, then the game's."""
    own = library.list_sounds(project)
    taken = {library.sound_key(n) for n in own}
    game = sorted((n for n in library.game_sound_files(project)
                   if library.sound_key(n) not in taken), key=str.lower)
    return own + game


def sound_file(project: Project, name: str) -> Path | None:
    """Where a picked sound is (library, game folder or game install)."""
    name = name.removesuffix(GAME_MARK).strip()
    return library.resolve(project, name) or library.game_sound_files(project).get(name)


class SoundPicker(QWidget):
    """An editable sound path with completion over the library and the
    game's sounds, and a button that plays it."""

    def __init__(self, project: Project, value: str, choices: list[str],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.box = QComboBox()
        self.box.setEditable(True)
        self.box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        own = {library.sound_key(n) for n in library.list_sounds(project)}
        for name in choices:
            self.box.addItem(name if library.sound_key(name) in own else name + GAME_MARK,
                             name)
        completer = QCompleter([self.box.itemText(i) for i in range(self.box.count())], self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.box.setCompleter(completer)
        self.box.setEditText(value)
        self.box.setToolTip("A path under sound/ — type to search the library and the game's "
                            "sounds")
        play = QToolButton()
        play.setIcon(icon("play"))
        play.setToolTip("Play it")
        play.clicked.connect(self.play)
        row.addWidget(self.box, 1)
        row.addWidget(play)

    def value(self) -> str:
        return self.box.currentText().removesuffix(GAME_MARK).strip()

    def set_value(self, name: str) -> None:
        self.box.setEditText(name)

    def play(self) -> None:
        path = sound_file(self.project, self.value())
        if path is not None:
            audio.play(path)


class ModelSoundsDialog(QDialog):
    """Each sound of one asset's events and what to play instead."""

    COLUMNS = ["Sound", "", "Plays in", "Play instead"]

    def __init__(self, project: Project, asset: str, sounds: dict[str, list[tuple[str, int]]],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.setWindowTitle(f"Sounds — {asset}")
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header(
            f"Sounds of {asset}",
            "Every sound the weapon's animation events play. Pick another to play instead "
            "(type to search the library and the game's sounds; ▶ plays it) — it replaces "
            "the sound in every sequence. Undo puts it back."))
        self.sounds = list(sounds)
        choices = sound_choices(project)
        self.table = QTableWidget(len(self.sounds), len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(2, 220)
        self.pickers: list[SoundPicker] = []
        for row, sound in enumerate(self.sounds):
            item = QTableWidgetItem(sound)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if sound_file(project, sound) is None:
                item.setForeground(theme.color("muted"))
                item.setToolTip("Not in the library or the game: the event plays nothing")
            self.table.setItem(row, 0, item)
            play = QToolButton()
            play.setIcon(icon("play"))
            play.setToolTip("Play the sound it plays now")
            play.clicked.connect(lambda _c=False, s=sound: self._play(s))
            self.table.setCellWidget(row, 1, play)
            uses = sounds[sound]
            where = ", ".join(f"{seq} @{frame}" for seq, frame in uses[:4])
            used = QTableWidgetItem(where + (f" +{len(uses) - 4}" if len(uses) > 4 else ""))
            used.setToolTip("\n".join(f"{seq} frame {frame}" for seq, frame in uses))
            used.setFlags(used.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 2, used)
            picker = SoundPicker(project, sound, choices)
            self.pickers.append(picker)
            self.table.setCellWidget(row, 3, picker)
        layout.addWidget(self.table, 1)
        add = QPushButton(icon("file-plus"), "Import a WAV for the selected sound…")
        add.setAutoDefault(False)
        add.clicked.connect(self._import)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(add)
        bottom.addStretch(1)
        bottom.addWidget(buttons)
        layout.addLayout(bottom)
        self.resize(980, 520)

    def _play(self, sound: str) -> None:
        path = sound_file(self.project, sound)
        if path is not None:
            audio.play(path)

    def _import(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            row = 0
        if not self.pickers:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import a WAV", "", "WAV (*.wav)")
        if not path:
            return
        names = library.import_sounds(self.project, [Path(path)])
        if names:
            self.pickers[row].set_value(names[0])

    def mapping(self) -> dict[str, str]:
        """``{sound: the one to play instead}`` for every changed row."""
        return {sound: picker.value() for sound, picker in zip(self.sounds, self.pickers,
                                                                strict=True)
                if picker.value() and picker.value().lower() != sound.lower()}


__all__ = ["ModelSoundsDialog", "SoundPicker", "sound_choices", "sound_file"]
