"""Inspector pages that edit the bone hierarchy and the attachments.

The pages only *request* edits (signals); the main window runs them as jobs
through :mod:`valve_qc_merger.project.bones`, snapshotting the asset first so
**Undo** can restore it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.project.bones import AttachmentSpec
from valve_qc_merger.studio.model_info import ModelInfo

ROOT_CHOICE = "(no parent — root)"


class BoneTree(QTreeWidget):
    """Hierarchy view; dragging a bone onto another asks to reparent it
    (onto empty space: make it a root). The tree itself is not changed —
    the edit runs, then the model reloads."""

    reparent_requested = Signal(str, object)  # bone, new parent name or None

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderLabels(["Bone", "#", "Vertices"])
        self.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def dropEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        dragged = self.currentItem()
        target = self.itemAt(event.position().toPoint())
        event.ignore()
        if dragged is None:
            return
        position = self.dropIndicatorPosition()
        on_item = QAbstractItemView.DropIndicatorPosition.OnItem
        if target is None or position == QAbstractItemView.DropIndicatorPosition.OnViewport:
            parent = None
        elif position == on_item:
            parent = target.text(0)
        else:  # above / below an item: become its sibling
            parent = target.parent().text(0) if target.parent() is not None else None
        current = dragged.parent().text(0) if dragged.parent() is not None else None
        if parent != current and parent != dragged.text(0):
            self.reparent_requested.emit(dragged.text(0), parent)


class BonesPage(QWidget):
    bone_selected = Signal(str)
    rename_requested = Signal(str)
    reparent_requested = Signal(str, object)
    reparent_dialog_requested = Signal(str)
    delete_requested = Signal(str)
    undo_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tree = BoneTree()
        self.tree.reparent_requested.connect(self.reparent_requested)
        self.tree.currentItemChanged.connect(
            lambda item, _prev: self.bone_selected.emit(item.text(0) if item else ""))
        row = QHBoxLayout()
        self.rename_button = QPushButton("Rename…")
        self.reparent_button = QPushButton("Change parent…")
        self.delete_button = QPushButton("Delete…")
        self.undo_button = QPushButton("Undo")
        self.rename_button.clicked.connect(lambda: self._emit(self.rename_requested))
        self.reparent_button.clicked.connect(lambda: self._emit(self.reparent_dialog_requested))
        self.delete_button.clicked.connect(lambda: self._emit(self.delete_requested))
        self.undo_button.clicked.connect(self.undo_requested)
        for button in (self.rename_button, self.reparent_button, self.delete_button,
                       self.undo_button):
            row.addWidget(button)
        hint = QLabel("Drag a bone onto another to make it its child (onto empty space: "
                      "a root). World poses are kept; vertices of a deleted bone move to "
                      "its parent.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8a8a8a")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree, 1)
        layout.addLayout(row)
        layout.addWidget(hint)
        self.set_info(None, can_undo=False)

    def _emit(self, signal: Signal) -> None:
        item = self.tree.currentItem()
        if item is not None:
            signal.emit(item.text(0))

    def selected(self) -> str:
        item = self.tree.currentItem()
        return item.text(0) if item is not None else ""

    def set_info(self, info: ModelInfo | None, *, can_undo: bool) -> None:
        keep = self.selected()
        self.tree.clear()
        items: dict[int, QTreeWidgetItem] = {}
        for bone in (info.bones if info is not None else []):
            item = QTreeWidgetItem([bone.name, str(bone.index),
                                    str(bone.vertices) if bone.vertices else ""])
            parent = items.get(bone.parent)
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            items[bone.index] = item
            if bone.name == keep:
                self.tree.setCurrentItem(item)
        self.tree.expandAll()
        has = info is not None and bool(info.bones)
        for button in (self.rename_button, self.reparent_button, self.delete_button):
            button.setEnabled(has)
        self.undo_button.setEnabled(can_undo)


class AttachmentsPage(QWidget):
    save_requested = Signal(list)  # list[AttachmentSpec]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["#", "Bone", "X", "Y", "Z"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._bones: list[str] = []
        row = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(lambda: self._add_row(AttachmentSpec(
            self._next_index(), self._bones[0] if self._bones else "", (0.0, 0.0, 0.0))))
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda: self.table.removeRow(self.table.currentRow()))
        self.save_button = QPushButton("Save attachments")
        self.save_button.clicked.connect(lambda: self.save_requested.emit(self.specs()))
        for button in (add, remove, self.save_button):
            row.addWidget(button)
        hint = QLabel("Offsets are in the bone's frame. GoldSource uses up to 4 "
                      "(0 = muzzle flash, 1 = shell eject on most weapons).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8a8a8a")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table, 1)
        layout.addLayout(row)
        layout.addWidget(hint)

    def _next_index(self) -> int:
        used = {s.index for s in self.specs()}
        return next(i for i in range(64) if i not in used)

    def _add_row(self, spec: AttachmentSpec) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        index = QSpinBox()
        index.setRange(0, 31)
        index.setValue(spec.index)
        self.table.setCellWidget(r, 0, index)
        bone = QComboBox()
        bone.addItems(self._bones)
        bone.setCurrentText(spec.bone)
        self.table.setCellWidget(r, 1, bone)
        for c, value in enumerate(spec.offset, start=2):
            spin = QDoubleSpinBox()
            spin.setRange(-1e7, 1e7)
            spin.setDecimals(4)
            spin.setValue(value)
            self.table.setCellWidget(r, c, spin)

    def set_info(self, info: ModelInfo | None) -> None:
        self.table.setRowCount(0)
        self._bones = [b.name for b in info.bones] if info is not None else []
        for att in (info.attachments if info is not None else []):
            self._add_row(AttachmentSpec(att.index, att.bone, att.offset))
        self.save_button.setEnabled(info is not None)

    def specs(self) -> list[AttachmentSpec]:
        out = []
        for r in range(self.table.rowCount()):
            out.append(AttachmentSpec(
                self.table.cellWidget(r, 0).value(),
                self.table.cellWidget(r, 1).currentText(),
                tuple(self.table.cellWidget(r, c).value() for c in (2, 3, 4)),
            ))
        return out


__all__ = ["AttachmentsPage", "BoneTree", "BonesPage", "ROOT_CHOICE"]
