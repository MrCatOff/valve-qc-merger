"""Explorer, Inspector and Log panels of the studio window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QFont, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.project import ASSET_KINDS, Project
from valve_qc_merger.studio.model_info import ModelInfo, texture_rgba

KIND_TITLES = {
    "v": "View models (v_)",
    "p": "Player-held (p_)",
    "w": "World (w_)",
    "player": "Player bodies",
    "zhands": "Zombie hands",
}
ROLE_KIND = Qt.ItemDataRole.UserRole
ROLE_NAME = Qt.ItemDataRole.UserRole + 1


# --------------------------------------------------------------------------- #
# Explorer
# --------------------------------------------------------------------------- #
class Explorer(QTreeWidget):
    """Project tree: assets grouped by kind, then builds."""

    asset_selected = Signal(str)  # asset name ("" when nothing / a group)
    build_selected = Signal(str)
    remove_requested = Signal(str)
    kind_change_requested = Signal(str, str)
    reveal_requested = Signal(str)
    build_run_requested = Signal(str)
    build_compile_requested = Signal(str)
    build_delete_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.currentItemChanged.connect(self._on_current)

    def show_project(self, project: Project | None) -> None:
        selected = self.current_asset()
        selected_build = self.current_build()
        self.clear()
        if project is None:
            return
        assets = QTreeWidgetItem(self, [f"Assets ({len(project.assets)})"])
        assets.setData(0, ROLE_KIND, "assets")
        for kind in ASSET_KINDS:
            members = sorted(a.name for a in project.assets.values() if a.kind == kind)
            if not members:
                continue
            group = QTreeWidgetItem(assets, [f"{KIND_TITLES[kind]}  ·  {len(members)}"])
            group.setData(0, ROLE_KIND, "group")
            for name in members:
                item = QTreeWidgetItem(group, [name])
                item.setData(0, ROLE_KIND, "asset")
                item.setData(0, ROLE_NAME, name)
                if name == selected:
                    self.setCurrentItem(item)
        builds = QTreeWidgetItem(self, [f"Builds ({len(project.builds)})"])
        builds.setData(0, ROLE_KIND, "builds")
        for name, build in sorted(project.builds.items()):
            item = QTreeWidgetItem(builds, [f"{name}  ·  {build.kind}"])
            item.setData(0, ROLE_KIND, "build")
            item.setData(0, ROLE_NAME, name)
            if name == selected_build:
                self.setCurrentItem(item)
        self.expandAll()

    def current_asset(self) -> str:
        item = self.currentItem()
        if item is not None and item.data(0, ROLE_KIND) == "asset":
            return str(item.data(0, ROLE_NAME))
        return ""

    def _on_current(self, item: QTreeWidgetItem | None, _previous: object) -> None:
        kind = item.data(0, ROLE_KIND) if item is not None else None
        if kind == "build":
            self.build_selected.emit(str(item.data(0, ROLE_NAME)))
        else:
            self.asset_selected.emit(self.current_asset())

    def current_build(self) -> str:
        item = self.currentItem()
        if item is not None and item.data(0, ROLE_KIND) == "build":
            return str(item.data(0, ROLE_NAME))
        return ""

    def select(self, kind: str, name: str) -> bool:
        """Make the asset/build item ``name`` current."""
        def walk(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            if item.data(0, ROLE_KIND) == kind and item.data(0, ROLE_NAME) == name:
                return item
            for i in range(item.childCount()):
                hit = walk(item.child(i))
                if hit is not None:
                    return hit
            return None
        for i in range(self.topLevelItemCount()):
            hit = walk(self.topLevelItem(i))
            if hit is not None:
                self.setCurrentItem(hit)
                return True
        return False

    def _context_menu(self, pos) -> None:  # noqa: ANN001 - QPoint
        item = self.itemAt(pos)
        if item is not None and item.data(0, ROLE_KIND) == "build":
            name = str(item.data(0, ROLE_NAME))
            menu = QMenu(self)
            menu.addAction("Run", lambda: self.build_run_requested.emit(name))
            menu.addAction("Compile", lambda: self.build_compile_requested.emit(name))
            menu.addSeparator()
            menu.addAction("Delete build…", lambda: self.build_delete_requested.emit(name))
            menu.exec(self.viewport().mapToGlobal(pos))
            return
        if item is None or item.data(0, ROLE_KIND) != "asset":
            return
        name = str(item.data(0, ROLE_NAME))
        menu = QMenu(self)
        kinds = menu.addMenu("Change kind")
        for kind in ASSET_KINDS:
            action = QAction(KIND_TITLES[kind], kinds)
            action.triggered.connect(
                lambda _c=False, k=kind: self.kind_change_requested.emit(name, k))
            kinds.addAction(action)
        menu.addAction("Show in folder", lambda: self.reveal_requested.emit(name))
        menu.addSeparator()
        menu.addAction("Remove from project…", lambda: self.remove_requested.emit(name))
        menu.exec(self.viewport().mapToGlobal(pos))


# --------------------------------------------------------------------------- #
# Inspector
# --------------------------------------------------------------------------- #
def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _fill(table: QTableWidget, rows: list[list[object]]) -> None:
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            item = QTableWidgetItem("" if value is None else str(value))
            if isinstance(value, (int, float)):
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(r, c, item)


class PixmapLabel(QLabel):
    """Shows a pixmap scaled to fit, re-scaled whenever the label resizes."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(64, 64)

    def set_image(self, pixmap: QPixmap | None) -> None:
        self._pixmap = pixmap
        self._rescale()

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            super().clear()
            return
        # nearest-neighbour keeps the 8-bit texels crisp when enlarged
        self.setPixmap(self._pixmap.scaled(
            self.width(), self.height(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation))


class Inspector(QTabWidget):
    """Tabs describing the selected asset."""

    kind_changed = Signal(str, str)  # asset, kind
    notes_changed = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._asset = ""
        overview = QWidget()
        form = QFormLayout(overview)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.name_label = QLabel("—")
        self.name_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.kind_box = QComboBox()
        for kind in ASSET_KINDS:
            self.kind_box.addItem(KIND_TITLES[kind], kind)
        self.kind_box.activated.connect(self._kind_activated)
        self.path_label = QLabel("—")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_label.setWordWrap(True)
        self.source_label = QLabel("—")
        self.source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.source_label.setWordWrap(True)
        self.stats_label = QLabel("—")
        self.stats_label.setWordWrap(True)
        self.notes = QLineEdit()
        self.notes.setPlaceholderText("notes")
        self.notes.editingFinished.connect(
            lambda: self._asset and self.notes_changed.emit(self._asset, self.notes.text()))
        self.warnings_label = QLabel("")
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setStyleSheet("color: #c58a00")
        form.addRow("Name", self.name_label)
        form.addRow("Kind", self.kind_box)
        form.addRow("Folder", self.path_label)
        form.addRow("Source", self.source_label)
        form.addRow("Contents", self.stats_label)
        form.addRow("Notes", self.notes)
        form.addRow("", self.warnings_label)
        self.addTab(overview, "Overview")

        self.submodels = _table(["Bodygroup", "Submodel", "Triangles", "Vertices", "Textures"])
        self.addTab(self.submodels, "Bodygroups")
        textures = QWidget()
        tex_layout = QVBoxLayout(textures)
        self.textures = _table(["Texture", "Size", "Render mode", "Used by"])
        self.textures.currentCellChanged.connect(self._show_texture)
        self.texture_preview = PixmapLabel()
        self.texture_preview.setMinimumHeight(160)
        tex_layout.addWidget(self.textures, 2)
        tex_layout.addWidget(self.texture_preview, 1)
        self.addTab(textures, "Textures")
        self.sequences = _table(["#", "Sequence", "FPS", "Frames", "Loop", "Events"])
        self.addTab(self.sequences, "Sequences")
        from valve_qc_merger.studio.bone_tools import AttachmentsPage, BonesPage
        self.bones_page = BonesPage()
        self.bones = self.bones_page.tree
        self.addTab(self.bones_page, "Bones")
        self.attachments_page = AttachmentsPage()
        self.attachments = self.attachments_page.table
        self.addTab(self.attachments_page, "Attachments")
        self._info: ModelInfo | None = None
        self.show_asset(None, None)

    def show_asset(self, project: Project | None, info: ModelInfo | None,
                   name: str = "") -> None:
        self._asset = name
        can_undo = bool(project is not None and name and project.can_undo(name))
        self.bones_page.set_info(info, can_undo=can_undo)
        self.attachments_page.set_info(info)
        self._info = info
        asset = project.assets.get(name) if project is not None and name else None
        enabled = asset is not None
        for widget in (self.kind_box, self.notes):
            widget.setEnabled(enabled)
        if asset is None:
            self.name_label.setText("—")
            self.path_label.setText("—")
            self.source_label.setText("—")
            self.stats_label.setText("Select an asset in the Explorer")
            self.notes.setText("")
            self.warnings_label.setText("")
            for table in (self.submodels, self.textures, self.sequences):
                table.setRowCount(0)
            self.texture_preview.set_image(None)
            return
        self.name_label.setText(asset.name)
        self.kind_box.setCurrentIndex(ASSET_KINDS.index(asset.kind))
        self.path_label.setText(str(project.root / asset.path))
        self.source_label.setText(asset.source or "—")
        self.notes.setText(asset.notes)
        if info is None:
            self.stats_label.setText("loading…")
            return
        self.stats_label.setText(
            f"{len(info.submodels)} submodels · {info.triangles} triangles · "
            f"{len(info.textures)} textures · {len(info.sequences)} sequences · "
            f"{len(info.bones)} bones · {len(info.attachments)} attachments")
        self.warnings_label.setText("\n".join(f"⚠ {w}" for w in info.warnings[:8]))
        _fill(self.submodels, [[s.group, s.stem, s.triangles, s.vertices,
                                ", ".join(s.materials)] for s in info.submodels])
        _fill(self.textures, [[t.name, f"{t.width}×{t.height}" if t.width else "?",
                               t.render_mode, ", ".join(t.used_by)] for t in info.textures])
        _fill(self.sequences, [[s.index, s.name, s.fps if s.fps is not None else "",
                                s.frames, "yes" if s.loop else "", len(s.events)]
                               for s in info.sequences])
        self.texture_preview.set_image(None)
        if info.textures:
            self.textures.setCurrentCell(0, 0)

    def _kind_activated(self, index: int) -> None:
        if self._asset:
            self.kind_changed.emit(self._asset, self.kind_box.itemData(index))

    def _show_texture(self, row: int, *_args: int) -> None:
        if self._info is None or not (0 <= row < len(self._info.textures)):
            self.texture_preview.set_image(None)
            return
        texture = self._info.textures[row]
        if texture.path is None:
            self.texture_preview.set_image(None)
            self.texture_preview.setText("texture file missing")
            return
        width, height, rgba = texture_rgba(texture.path,
                                           masked=texture.render_mode == "masked")
        image = QImage(rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
        self.texture_preview.set_image(QPixmap.fromImage(image))


# --------------------------------------------------------------------------- #
# Log
# --------------------------------------------------------------------------- #
class LogPanel(QPlainTextEdit):
    """Read-only, monospaced job log (bounded)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(20_000)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

    def append_line(self, line: str) -> None:
        self.appendPlainText(line)


def project_title(project: Project | None, path: Path | None = None) -> str:
    if project is None:
        return "valve-qc-merger Studio"
    return f"{project.name} — valve-qc-merger Studio"


__all__ = ["Explorer", "Inspector", "KIND_TITLES", "LogPanel", "project_title"]
