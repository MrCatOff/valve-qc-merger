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
ROLE_CATEGORY = Qt.ItemDataRole.UserRole + 2  # the category a row sits in
UNCATEGORIZED = "Uncategorized"
NEW_CATEGORY = "\x00new"  # move_to_category target: ask for a new name


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
    build_run_compile_requested = Signal(str)
    build_delete_requested = Signal(str)
    derive_requested = Signal(list)  # asset names: open the Retarget dialog
    rederive_requested = Signal(str, bool)  # derived asset, edit settings first
    category_move_requested = Signal(list, str)  # assets, category ("" / NEW_CATEGORY)
    category_new_requested = Signal()
    category_rename_requested = Signal(str)
    category_delete_requested = Signal(str)
    category_builds_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # drag assets onto a category (handled in dropEvent; nothing moves by itself)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.currentItemChanged.connect(self._on_current)
        self._project: Project | None = None

    def show_project(self, project: Project | None) -> None:
        self._project = project
        selected = self.current_asset()
        selected_build = self.current_build()
        self.clear()
        if project is None:
            return
        assets = QTreeWidgetItem(self, [f"Assets ({len(project.assets)})"])
        assets.setData(0, ROLE_KIND, "assets")
        if project.categories:
            uncategorized = [a for a in project.assets.values() if not a.category]
            names = sorted(project.categories, key=str.lower)
            for category in names + ([""] if uncategorized else []):
                count = sum(1 for a in project.assets.values() if a.category == category)
                node = QTreeWidgetItem(assets, [f"{category or UNCATEGORIZED}  ·  {count}"])
                node.setData(0, ROLE_KIND, "category")
                node.setData(0, ROLE_NAME, category)
                node.setData(0, ROLE_CATEGORY, category)
                self._fill_kinds(node, project, category, selected)
        else:
            self._fill_kinds(assets, project, None, selected)
        builds = QTreeWidgetItem(self, [f"Builds ({len(project.builds)})"])
        builds.setData(0, ROLE_KIND, "builds")
        for name, build in sorted(project.builds.items()):
            item = QTreeWidgetItem(builds, [f"{name}  ·  {build.kind}"])
            item.setData(0, ROLE_KIND, "build")
            item.setData(0, ROLE_NAME, name)
            if name == selected_build:
                self.setCurrentItem(item)
        self.expandAll()

    def _fill_kinds(self, parent: QTreeWidgetItem, project: Project,
                    category: str | None, selected: str) -> None:
        """Kind groups with their assets (of one category, or all)."""
        for kind in ASSET_KINDS:
            members = sorted(a.name for a in project.assets.values() if a.kind == kind
                             and (category is None or a.category == category))
            if not members:
                continue
            group = QTreeWidgetItem(parent, [f"{KIND_TITLES[kind]}  ·  {len(members)}"])
            group.setData(0, ROLE_KIND, "group")
            group.setData(0, ROLE_CATEGORY, category)
            for name in members:
                derived = project.assets[name].derived
                item = QTreeWidgetItem(group, [f"{name}  ↳ {derived['from']}" if derived
                                               else name])
                item.setData(0, ROLE_KIND, "asset")
                item.setData(0, ROLE_NAME, name)
                item.setData(0, ROLE_CATEGORY, category)
                if derived:
                    item.setToolTip(0, f"{derived['mode']} from {derived['from']}")
                if name == selected:
                    self.setCurrentItem(item)

    def current_category(self) -> str | None:
        """The category of the current row (None: no categories / not inside one)."""
        item = self.currentItem()
        return item.data(0, ROLE_CATEGORY) if item is not None else None

    # -- drag & drop onto a category -----------------------------------------
    def _drop_category(self, pos) -> str | None:  # noqa: ANN001 - QPoint
        item = self.itemAt(pos)
        if item is None or item.data(0, ROLE_KIND) not in ("category", "group", "asset"):
            return None
        return item.data(0, ROLE_CATEGORY)

    def dragMoveEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        if self.selected_assets() and self._drop_category(event.position().toPoint()) \
                is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        category = self._drop_category(event.position().toPoint())
        names = self.selected_assets()
        event.ignore()  # the tree is rebuilt from the project, never moved by Qt
        if category is not None and names:
            self.category_move_requested.emit(names, category)

    def current_asset(self) -> str:
        item = self.currentItem()
        if item is not None and item.data(0, ROLE_KIND) == "asset":
            return str(item.data(0, ROLE_NAME))
        return ""

    def selected_assets(self) -> list[str]:
        """Every selected asset (the current one first)."""
        names = [str(i.data(0, ROLE_NAME)) for i in self.selectedItems()
                 if i.data(0, ROLE_KIND) == "asset"]
        current = self.current_asset()
        if current:
            names = [current] + [n for n in names if n != current]
        return names

    def _on_current(self, item: QTreeWidgetItem | None, _previous: object) -> None:
        kind = item.data(0, ROLE_KIND) if item is not None else None
        if kind == "build":
            self.build_selected.emit(str(item.data(0, ROLE_NAME)))
        else:
            self.asset_selected.emit(self.current_asset())

    def _derived(self, name: str) -> bool:
        return bool(self._project is not None and name in self._project.assets
                    and self._project.assets[name].derived)

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
        if item is not None and item.data(0, ROLE_KIND) in ("assets", "category"):
            menu = QMenu(self)
            menu.addAction("New category…", self.category_new_requested.emit)
            category = item.data(0, ROLE_NAME) if item.data(0, ROLE_KIND) == "category" \
                else None
            if category:
                menu.addAction("Create builds for this category",
                               lambda: self.category_builds_requested.emit(category))
                menu.addSeparator()
                menu.addAction("Rename category…",
                               lambda: self.category_rename_requested.emit(category))
                menu.addAction("Delete category (assets stay)…",
                               lambda: self.category_delete_requested.emit(category))
            menu.exec(self.viewport().mapToGlobal(pos))
            return
        if item is not None and item.data(0, ROLE_KIND) == "build":
            name = str(item.data(0, ROLE_NAME))
            menu = QMenu(self)
            menu.addAction("Run", lambda: self.build_run_requested.emit(name))
            menu.addAction("Compile", lambda: self.build_compile_requested.emit(name))
            menu.addAction("Run and compile",
                           lambda: self.build_run_compile_requested.emit(name))
            menu.addSeparator()
            menu.addAction("Delete build…", lambda: self.build_delete_requested.emit(name))
            menu.exec(self.viewport().mapToGlobal(pos))
            return
        if item is None or item.data(0, ROLE_KIND) != "asset":
            return
        name = str(item.data(0, ROLE_NAME))
        selected = self.selected_assets()
        if name not in selected:
            selected = [name]
        menu = QMenu(self)
        title = "Retarget…" if len(selected) == 1 else f"Retarget {len(selected)} assets…"
        menu.addAction(title, lambda: self.derive_requested.emit(selected))
        if self._derived(name):
            menu.addAction("Re-run retarget",
                           lambda: self.rederive_requested.emit(name, False))
            menu.addAction("Retarget settings…",
                           lambda: self.rederive_requested.emit(name, True))
        menu.addSeparator()
        move = menu.addMenu("Move to category")
        current = self._project.assets[name].category if self._project else ""
        for category in sorted(self._project.categories if self._project else [],
                               key=str.lower):
            action = move.addAction(category,
                                    lambda c=category: self.category_move_requested.emit(
                                        selected, c))
            action.setEnabled(category != current or len(selected) > 1)
        move.addAction("New category…",
                       lambda: self.category_move_requested.emit(selected, NEW_CATEGORY))
        if self._project and self._project.categories:
            move.addSeparator()
            move.addAction(UNCATEGORIZED,
                           lambda: self.category_move_requested.emit(selected, ""))
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
        self.category_label = QLabel("—")
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
        form.addRow("Category", self.category_label)
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
            self.category_label.setText("—")
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
        self.category_label.setText(asset.category or UNCATEGORIZED)
        self.kind_box.setCurrentIndex(ASSET_KINDS.index(asset.kind))
        self.path_label.setText(str(project.root / asset.path))
        derived = asset.derived
        if derived:
            options = ", ".join(f"{k}={v}" for k, v in derived.get("options", {}).items())
            self.source_label.setText(
                f"{derived['mode']} from asset {derived['from']}"
                + (f" ({options})" if options else ""))
        else:
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
