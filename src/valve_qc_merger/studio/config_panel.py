"""Server ▸ Config: edit ``game.cfg`` (ReGameDLL) and ``server.cfg`` (ReHLDS).

Every cvar comes with its description, options and default from the bundled
references, so a stripped file is still documented. Values change in place —
comments and unknown lines survive; the old file is kept as ``.bak``.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.project import Project
from valve_qc_merger.resources import data_root
from valve_qc_merger.server import cfg
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import icon

FILES = {"game": "game.cfg", "server": "server.cfg"}
NAME, VALUE, DEFAULT, ABOUT = range(4)


def _cell(text: str, *, editable: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if not editable:
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


def _tooltip(cvar: cfg.Cvar) -> str:
    lines = [cvar.description] if cvar.description else []
    lines += [f"{value} — {text}" for value, text in cvar.options]
    lines += cvar.notes
    if cvar.default is not None:
        lines.append(f'Default: "{cvar.default}"')
    return "\n".join(lines)


class ConfigPanel(QWidget):
    """Pick a cfg, filter, edit values in the table, save."""

    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.cvars: list[cfg.Cvar] = []
        self.loaded: dict[str, str] = {}  # name -> value as read (what the file sets)
        self._filling = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 12, 4, 4)
        layout.setSpacing(8)
        layout.addWidget(kit.hint(
            "Every cvar with its description and default (from ReGameDLL's game.cfg and the "
            "ReHLDS README). Edit the Value column; Save changes only those lines — "
            "comments and anything else in the file stay, the old file is kept as .bak."))
        row = QHBoxLayout()
        self.kind_box = QComboBox()
        for kind, (title, _file) in cfg.REFERENCES.items():
            self.kind_box.addItem(title, kind)
        self.kind_box.currentIndexChanged.connect(lambda _i: self._kind_changed())
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("cfg file, e.g. …/cstrike/game.cfg")
        self.path_edit.editingFinished.connect(self.load)
        pick = QPushButton(icon("folder-open"), "")
        pick.setToolTip("Choose the file")
        pick.clicked.connect(self._pick)
        reload_ = QPushButton(icon("refresh-cw"), "")
        reload_.setToolTip("Read the file again (drops unsaved edits)")
        reload_.clicked.connect(self.load)
        for button in (pick, reload_):
            button.setAutoDefault(False)
        row.addWidget(self.kind_box)
        row.addWidget(self.path_edit, 1)
        row.addWidget(pick)
        row.addWidget(reload_)
        layout.addLayout(row)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search cvars and descriptions")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._apply_filter())
        self.group_box = QComboBox()
        self.group_box.addItem("All groups", "")
        for key, title in cfg.GROUPS.items():
            self.group_box.addItem(title, key)
        self.group_box.currentIndexChanged.connect(lambda _i: self._apply_filter())
        self.changed_box = QCheckBox("Only non-default")
        self.changed_box.toggled.connect(lambda _on: self._apply_filter())
        filters.addWidget(self.search, 1)
        filters.addWidget(self.group_box)
        filters.addWidget(self.changed_box)
        layout.addLayout(filters)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Cvar", "Value", "Default", "Description"])
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(NAME, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(VALUE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(DEFAULT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(ABOUT, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(NAME, 230)
        self.table.setColumnWidth(VALUE, 110)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed
                                   | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.table.itemChanged.connect(self._edited)
        self.table.currentCellChanged.connect(lambda *_a: self._show_details())
        layout.addWidget(self.table, 1)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.details.setMinimumHeight(64)
        theme.set_role(self.details, "muted")
        layout.addWidget(self.details)

        bottom = QHBoxLayout()
        self.status = QLabel()
        reset = QPushButton(icon("eraser"), "Reset to default")
        reset.setToolTip("Set the selected cvar back to its default value")
        reset.clicked.connect(self.reset_selected)
        save_as = QPushButton(icon("file-down"), "Save as…")
        save_as.clicked.connect(self.save_as)
        self.save_button = QPushButton(icon("save", theme.TOKENS["on_accent"]), "Save")
        theme.set_primary(self.save_button)
        self.save_button.clicked.connect(lambda: self.save())
        for button in (reset, save_as, self.save_button):
            button.setAutoDefault(False)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(reset)
        bottom.addWidget(save_as)
        bottom.addWidget(self.save_button)
        layout.addLayout(bottom)
        self._kind_changed()

    # -- loading -------------------------------------------------------------
    @property
    def kind(self) -> str:
        return self.kind_box.currentData()

    def default_path(self) -> Path | None:
        game = self.project.settings.game_dir
        return Path(game) / FILES[self.kind] if game else None

    def _kind_changed(self) -> None:
        path = self.default_path()
        self.path_edit.setText(str(path) if path else "")
        self.load()

    def _pick(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Config file", self.path_edit.text(),
                                              "Config (*.cfg);;All files (*)")
        if path:
            self.path_edit.setText(path)
            self.load()

    def path(self) -> Path | None:
        text = self.path_edit.text().strip()
        return Path(text) if text else None

    def load(self) -> None:
        path = self.path()
        text = path.read_text(encoding="utf-8", errors="replace") \
            if path and path.is_file() else ""
        self.cvars = cfg.documented(text, self.kind)
        self.loaded = {c.name.lower(): c.value for c in cfg.parse(text)}
        self._fill()
        if path is None:
            self.status.setText("No file chosen — showing the defaults; Save as… writes one.")
        elif path.is_file():
            self.status.setText(f"{len(self.loaded)} cvar(s) set in {path.name}.")
        else:
            self.status.setText(f"{path.name} does not exist yet — Save creates it.")

    def _fill(self) -> None:
        self._filling = True
        self.table.setRowCount(len(self.cvars))
        for row, cvar in enumerate(self.cvars):
            tip = _tooltip(cvar)
            name = _cell(cvar.name)
            name.setToolTip(tip)
            name.setData(Qt.ItemDataRole.UserRole, row)
            self.table.setItem(row, NAME, name)
            value = _cell(cvar.value, editable=True)
            value.setToolTip(tip)
            self.table.setItem(row, VALUE, value)
            self.table.setItem(row, DEFAULT, _cell("" if cvar.default is None
                                                   else cvar.default))
            first = cvar.description.splitlines()[0] if cvar.description else ""
            about = _cell(first)
            about.setToolTip(tip)
            self.table.setItem(row, ABOUT, about)
            self._paint(row)
        self._filling = False
        self._apply_filter()

    def _paint(self, row: int) -> None:
        cvar = self.cvars[row]
        name = self.table.item(row, NAME)
        value = self.table.item(row, VALUE)
        edited = self._is_edit(cvar)
        token = "warning" if edited else "accent" if cvar.changed else "text"
        for item in (name, value):
            item.setForeground(theme.color(token))
        font = name.font()
        font.setBold(edited)
        name.setFont(font)

    # -- filtering / details ---------------------------------------------------
    def _apply_filter(self) -> None:
        needle = self.search.text().strip().lower()
        group = self.group_box.currentData()
        only_changed = self.changed_box.isChecked()
        for row, cvar in enumerate(self.cvars):
            hidden = ((group and cvar.group != group)
                      or (only_changed and not cvar.changed)
                      or (needle and needle not in cvar.name.lower()
                          and needle not in cvar.description.lower()))
            self.table.setRowHidden(row, bool(hidden))

    def visible_names(self) -> list[str]:
        return [c.name for row, c in enumerate(self.cvars) if not self.table.isRowHidden(row)]

    def _show_details(self) -> None:
        row = self.table.currentRow()
        if not 0 <= row < len(self.cvars):
            self.details.setText("")
            return
        cvar = self.cvars[row]
        self.details.setText(f"<b>{cvar.name}</b><br>"
                             + _tooltip(cvar).replace("&", "&amp;").replace("<", "&lt;")
                             .replace("\n", "<br>"))

    # -- editing -------------------------------------------------------------
    def _edited(self, item: QTableWidgetItem) -> None:
        if self._filling or item.column() != VALUE:
            return
        cvar = self.cvars[item.row()]
        cvar.value = item.text().strip().strip('"')
        self._paint(item.row())
        self._update_status()

    def set_value(self, name: str, value: str) -> None:
        for row, cvar in enumerate(self.cvars):
            if cvar.name.lower() == name.lower():
                self.table.item(row, VALUE).setText(value)  # -> _edited
                return
        raise KeyError(name)

    def reset_selected(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self.cvars) and self.cvars[row].default is not None:
            self.table.item(row, VALUE).setText(self.cvars[row].default)

    def edits(self) -> list[cfg.Cvar]:
        """Cvars whose value differs from what the file sets (or would get by
        default when the file does not set them)."""
        return [c for c in self.cvars if self._is_edit(c)]

    def _is_edit(self, cvar: cfg.Cvar) -> bool:
        return self.loaded.get(cvar.name.lower(), cvar.default) != cvar.value

    def _update_status(self) -> None:
        count = len(self.edits())
        self.status.setText(f"{count} unsaved change(s)." if count else "No unsaved changes.")

    # -- saving --------------------------------------------------------------
    def _base(self, path: Path) -> cfg.CfgFile:
        if path.is_file():
            return cfg.CfgFile.load(path)
        if self.kind == "game":  # a new game.cfg starts as the documented reference
            ref = data_root() / "storage" / "server" / cfg.REFERENCES["game"][1]
            lines = ref.read_text(encoding="utf-8").splitlines()
            while lines and (lines[0].startswith("//") or not lines[0].strip()):
                lines.pop(0)  # our attribution header
            return cfg.CfgFile("\n".join(lines))
        return cfg.CfgFile()

    def save(self, path: Path | None = None) -> Path | None:
        path = path or self.path()
        if path is None:
            return self.save_as()
        document = self._base(path)
        # a new game.cfg starts from the reference: give every cvar the value shown
        changes = self.cvars if not path.is_file() and self.kind == "game" else self.edits()
        for cvar in changes:
            document.set(cvar.name, cvar.value, doc=cvar)
        backup = document.save(path)
        self.path_edit.setText(str(path))
        self.load()
        self.status.setText(f"Saved {path}" + (f" (previous: {backup.name})" if backup else ""))
        return path

    def save_as(self) -> Path | None:
        start = self.path_edit.text() or str(self.project.root / FILES[self.kind])
        path, _ = QFileDialog.getSaveFileName(self, "Save config", start, "Config (*.cfg)")
        return self.save(Path(path)) if path else None


__all__ = ["ConfigPanel"]
