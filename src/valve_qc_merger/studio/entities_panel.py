"""Server ▸ Entities: browse a map's entities, remove or replace them, save
as a ReHLDS ``.ent`` file or into the BSP."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.project import Project
from valve_qc_merger.server import entities as ents
from valve_qc_merger.server.bsp import BspError
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import icon

ALL = "All entities"
SUMMARY_KEYS = ("model", "targetname", "item", "target", "message")


def _cell(text: str, editable: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if not editable:
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


class ReplaceDialog(QDialog):
    """What the selected entities become: a model, a marker, nothing."""

    def __init__(self, count: int, classname: str, model: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Replace entities")
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header(
            f"Replace {count} entit{'y' if count == 1 else 'ies'}",
            f"{classname or 'The selection'}: each keeps its place (origin, angles)."))
        self.group = QButtonGroup(self)
        self.model_radio = QRadioButton("A model of your own (cycler_sprite: drawn, "
                                        "not picked up)")
        self.marker_radio = QRadioButton("A marker for a plugin (info_target)")
        self.remove_radio = QRadioButton("Nothing — remove them")
        for index, radio in enumerate((self.model_radio, self.marker_radio,
                                       self.remove_radio)):
            self.group.addButton(radio, index)
        self.model_radio.setChecked(True)
        self.model_edit = QLineEdit(model or "models/w_supplybox.mdl")
        self.name_edit = QLineEdit("vqm_supplybox")
        form = kit.form()
        form.addRow(self.model_radio)
        form.addRow("Model", self.model_edit)
        form.addRow(self.marker_radio)
        form.addRow("Name", self.name_edit)
        form.addRow(self.remove_radio)
        layout.addLayout(form)
        layout.addWidget(kit.hint(
            "A ZM server swaps dropped-weapon spawns (armoury_entity) for supply boxes: "
            "a model shows the box where the weapon lay (your plugin hands out the loot), "
            "a marker gives the plugin the spots by name. A new model takes a model slot."))
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Ok).setText("Replace")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self.resize(560, self.sizeHint().height())

    def mode(self) -> str:
        return ("model", "marker", "remove")[self.group.checkedId()]


class EntitiesPanel(QWidget):
    """Map picker, classes, the entities of a class, the keys of one."""

    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.map_path: Path | None = None
        self.entities: list[dict[str, str]] = []
        self.original: list[dict[str, str]] = []
        self.source = ""  # "bsp" or "ent"
        self._filling = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 12, 4, 4)
        layout.setSpacing(8)
        layout.addWidget(kit.hint(
            "A map's entities: remove them or replace them (e.g. dropped weapons with a "
            "supply box). Save as maps/<map>.ent for ReHLDS (sv_use_entity_file 1; the "
            "BSP stays as it is) or into the BSP — the map's CRC skips entities, so "
            "players with the original map still join."))
        row = QHBoxLayout()
        self.map_box = QComboBox()
        self.map_box.setMinimumWidth(220)
        self.map_box.currentIndexChanged.connect(lambda _i: self._map_chosen())
        open_button = QPushButton(icon("folder-open"), "Open BSP…")
        open_button.clicked.connect(self._open)
        self.source_label = QLabel()
        theme.set_role(self.source_label, "muted")
        open_button.setAutoDefault(False)
        row.addWidget(self.map_box)
        row.addWidget(open_button)
        row.addWidget(self.source_label, 1)
        layout.addLayout(row)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.class_list = QListWidget()
        self.class_list.currentItemChanged.connect(lambda *_a: self._fill_entities())
        split.addWidget(self.class_list)
        right = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Class", "Origin", "Model / name"])
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in (0, 1, 2):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.currentCellChanged.connect(lambda *_a: self._fill_keys())
        right.addWidget(self.table)
        keys_box = QWidget()
        keys_layout = QVBoxLayout(keys_box)
        keys_layout.setContentsMargins(0, 0, 0, 0)
        self.keys = QTableWidget(0, 2)
        self.keys.setHorizontalHeaderLabels(["Key", "Value"])
        self.keys.verticalHeader().setVisible(False)
        self.keys.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.keys.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.keys.itemChanged.connect(self._key_edited)
        keys_layout.addWidget(self.keys)
        key_row = QHBoxLayout()
        add_key = QPushButton(icon("file-plus"), "Add key")
        add_key.clicked.connect(self._add_key)
        remove_key = QPushButton(icon("minus"), "Remove key")
        remove_key.clicked.connect(self._remove_key)
        for button in (add_key, remove_key):
            button.setAutoDefault(False)
            key_row.addWidget(button)
        key_row.addStretch(1)
        keys_layout.addLayout(key_row)
        right.addWidget(keys_box)
        right.setSizes([300, 180])
        split.addWidget(right)
        split.setSizes([220, 560])
        layout.addWidget(split, 1)

        bottom = QHBoxLayout()
        self.status = QLabel()
        self.remove_button = QPushButton(icon("trash-2"), "Remove")
        self.remove_button.clicked.connect(self.remove_selected)
        self.replace_button = QPushButton(icon("repeat"), "Replace…")
        self.replace_button.clicked.connect(self.replace_selected)
        revert = QPushButton(icon("step-back"), "Revert")
        revert.setToolTip("Drop every unsaved change")
        revert.clicked.connect(self.revert)
        self.ent_button = QPushButton(icon("save", theme.TOKENS["on_accent"]), "Save .ent")
        theme.set_primary(self.ent_button)
        self.ent_button.setToolTip("Write maps/<map>.ent next to the map (ReHLDS "
                                   "sv_use_entity_file 1)")
        self.ent_button.clicked.connect(lambda: self.save_ent())
        self.bsp_button = QPushButton(icon("file-down"), "Save BSP…")
        self.bsp_button.clicked.connect(lambda: self.save_bsp())
        for button in (self.remove_button, self.replace_button, revert, self.ent_button,
                       self.bsp_button):
            button.setAutoDefault(False)
        layout.addWidget(self.status)
        bottom.addStretch(1)
        for button in (self.remove_button, self.replace_button, revert, self.bsp_button,
                       self.ent_button):
            bottom.addWidget(button)
        layout.addLayout(bottom)
        self._list_maps()

    # -- maps ----------------------------------------------------------------
    def maps_dir(self) -> Path | None:
        game = self.project.settings.game_dir
        return Path(game) / "maps" if game else None

    def _list_maps(self) -> None:
        self.map_box.blockSignals(True)
        self.map_box.clear()
        folder = self.maps_dir()
        maps = sorted(folder.glob("*.bsp"), key=lambda p: p.name.lower()) \
            if folder and folder.is_dir() else []
        if not maps:
            self.map_box.addItem("No maps — open a BSP", None)
        for path in maps:
            self.map_box.addItem(path.stem, str(path))
        self.map_box.blockSignals(False)
        self._map_chosen()

    def _open(self) -> None:
        start = str(self.maps_dir() or Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Open map", start, "Maps (*.bsp)")
        if path:
            self.map_box.blockSignals(True)
            if self.map_box.findData(None) >= 0 and self.map_box.count() == 1:
                self.map_box.clear()
            index = self.map_box.findData(path)
            if index < 0:
                self.map_box.addItem(Path(path).stem, path)
                index = self.map_box.count() - 1
            self.map_box.setCurrentIndex(index)
            self.map_box.blockSignals(False)
            self.load(Path(path))

    def _map_chosen(self) -> None:
        data = self.map_box.currentData()
        if data:
            self.load(Path(data))
        else:
            self.load(None)

    def ent_path(self) -> Path | None:
        return self.map_path.with_suffix(".ent") if self.map_path else None

    def load(self, path: Path | None) -> None:
        self.map_path, self.entities, self.original, self.source = path, [], [], ""
        if path is not None:
            try:
                ent = self.ent_path()
                if ent is not None and ent.is_file():
                    self.original = ents.read_ent_file(ent)
                    self.source = "ent"
                else:
                    self.original = ents.read_entities(path).entities
                    self.source = "bsp"
            except (BspError, OSError) as exc:
                self.source_label.setText(str(exc))
                self.map_path = None
        self.entities = [dict(e) for e in self.original]
        if self.map_path is not None:
            self.source_label.setText(
                f"from {self.map_path.stem}.ent (ReHLDS reads it instead of the BSP's)"
                if self.source == "ent" else f"from {self.map_path.name}")
        self._fill_classes()

    # -- views ---------------------------------------------------------------
    def classes(self) -> dict[str, int]:
        return ents.MapEntities(self.map_path or Path(), self.entities).classes()

    def _fill_classes(self, keep: str | None = None) -> None:
        keep = keep if keep is not None else self.current_class()
        self.class_list.blockSignals(True)
        self.class_list.clear()
        all_item = QListWidgetItem(f"{ALL}  ·  {len(self.entities)}")
        all_item.setData(Qt.ItemDataRole.UserRole, "")
        self.class_list.addItem(all_item)
        chosen = all_item
        for name, count in self.classes().items():
            item = QListWidgetItem(f"{name}  ·  {count}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.class_list.addItem(item)
            if name == keep:
                chosen = item
        self.class_list.setCurrentItem(chosen)
        self.class_list.blockSignals(False)
        self._fill_entities()

    def current_class(self) -> str:
        item = self.class_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else ""

    def select_class(self, name: str) -> None:
        for row in range(self.class_list.count()):
            if self.class_list.item(row).data(Qt.ItemDataRole.UserRole) == name:
                self.class_list.setCurrentRow(row)
                return
        raise KeyError(name)

    def _fill_entities(self) -> None:
        wanted = self.current_class()
        shown = [i for i, e in enumerate(self.entities)
                 if not wanted or e.get("classname", "") == wanted]
        self.table.setRowCount(len(shown))
        for row, index in enumerate(shown):
            entity = self.entities[index]
            number = _cell(str(index))
            number.setData(Qt.ItemDataRole.UserRole, index)
            self.table.setItem(row, 0, number)
            self.table.setItem(row, 1, _cell(entity.get("classname", "")))
            self.table.setItem(row, 2, _cell(entity.get("origin", "")))
            summary = next((f"{k}: {entity[k]}" for k in SUMMARY_KEYS if entity.get(k)), "")
            self.table.setItem(row, 3, _cell(summary))
        if shown:
            self.table.selectRow(0)
        self._fill_keys()
        self._update_status()

    def selected(self) -> list[int]:
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        return sorted(self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows)

    def select_all_shown(self) -> None:
        self.table.selectAll()

    def _current_entity(self) -> dict[str, str] | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return None if item is None else self.entities[item.data(Qt.ItemDataRole.UserRole)]

    def _fill_keys(self) -> None:
        self._filling = True
        entity = self._current_entity() or {}
        self.keys.setRowCount(len(entity))
        for row, (key, value) in enumerate(entity.items()):
            self.keys.setItem(row, 0, _cell(key))
            self.keys.setItem(row, 1, _cell(value, editable=True))
        self._filling = False

    def _key_edited(self, item: QTableWidgetItem) -> None:
        entity = self._current_entity()
        if self._filling or entity is None or item.column() != 1:
            return
        key = self.keys.item(item.row(), 0).text()
        entity[key] = item.text().replace('"', "'")
        self._update_status()

    def _add_key(self) -> None:
        entity = self._current_entity()
        if entity is None:
            return
        name = "key"
        number = 1
        while name in entity:
            number += 1
            name = f"key{number}"
        entity[name] = ""
        self._fill_keys()
        self.keys.setCurrentCell(self.keys.rowCount() - 1, 1)
        # the key's name is edited in place: make that cell editable once
        cell = self.keys.item(self.keys.rowCount() - 1, 0)
        cell.setFlags(cell.flags() | Qt.ItemFlag.ItemIsEditable)
        self.keys.itemChanged.connect(self._key_renamed)

    def _key_renamed(self, item: QTableWidgetItem) -> None:
        entity = self._current_entity()
        if item.column() != 0 or entity is None:
            return
        self.keys.itemChanged.disconnect(self._key_renamed)
        old = list(entity)[item.row()]
        new = item.text().strip().replace('"', "")
        if new and new != old and new not in entity:
            items = [((new if k == old else k), v) for k, v in entity.items()]
            entity.clear()
            entity.update(items)
        self._fill_keys()

    def _remove_key(self) -> None:
        entity = self._current_entity()
        row = self.keys.currentRow()
        if entity is None or row < 0:
            return
        key = self.keys.item(row, 0).text()
        if key != "classname":
            entity.pop(key, None)
            self._fill_keys()
            self._update_status()

    # -- edits ---------------------------------------------------------------
    def changed(self) -> bool:
        return self.entities != self.original

    def _update_status(self) -> None:
        if self.map_path is None:
            self.status.setText("")
            return
        before, after = ents.models_of(self.original), ents.models_of(self.entities)
        added, gone = len(after - before), len(before - after)
        slots = (f"; model slots {'+' if added >= gone else ''}{added - gone}"
                 if added or gone else "")
        delta = len(self.entities) - len(self.original)
        self.status.setText(
            f"{len(self.entities)} entities"
            + (f" ({delta:+d})" if delta else "")
            + slots + ("  ·  unsaved changes" if self.changed() else ""))
        theme.set_role(self.status, "warning" if self.changed() else "muted")

    def remove_selected(self) -> int:
        chosen = set(self.selected())
        chosen = {i for i in chosen if self.entities[i].get("classname") != "worldspawn"}
        if not chosen:
            return 0
        self.entities = [e for i, e in enumerate(self.entities) if i not in chosen]
        self._fill_classes()
        return len(chosen)

    def replace_selected(self, mode: str | None = None, value: str = "") -> int:
        chosen = [i for i in self.selected()
                  if self.entities[i].get("classname") != "worldspawn"]
        if not chosen:
            return 0
        if mode is None:
            dialog = ReplaceDialog(len(chosen), self.current_class(),
                                   self.project.settings.unprecache_replace, self)
            if dialog.exec() != ReplaceDialog.DialogCode.Accepted:
                return 0
            mode = dialog.mode()
            value = (dialog.model_edit.text() if mode == "model"
                     else dialog.name_edit.text()).strip()
        if mode == "remove":
            return self.remove_selected()
        make = ents.as_model if mode == "model" else ents.as_marker
        for index in chosen:
            self.entities[index] = make(self.entities[index], value)
        self._fill_classes()
        return len(chosen)

    def revert(self) -> None:
        self.entities = [dict(e) for e in self.original]
        self._fill_classes()

    # -- saving --------------------------------------------------------------
    def save_ent(self) -> Path | None:
        path = self.ent_path()
        if path is None:
            return None
        ents.write_ent_file(self.entities, path)
        self.original, self.source = [dict(e) for e in self.entities], "ent"
        self.source_label.setText(f"saved {path.name} — set sv_use_entity_file 1 in "
                                  "server.cfg")
        self._update_status()
        return path

    def save_bsp(self, target: Path | None = None) -> Path | None:
        if self.map_path is None:
            return None
        if target is None:
            chosen, _ = QFileDialog.getSaveFileName(self, "Save map", str(self.map_path),
                                                    "Maps (*.bsp)")
            if not chosen:
                return None
            target = Path(chosen)
        written, backup = ents.save_bsp(self.map_path, self.entities, target)
        if written.resolve() == self.map_path.resolve():
            self.original = [dict(e) for e in self.entities]
        self.source_label.setText(f"saved {written.name}"
                                  + (f" (original kept as {backup.name})" if backup else ""))
        self._update_status()
        return written


__all__ = ["EntitiesPanel", "ReplaceDialog"]
