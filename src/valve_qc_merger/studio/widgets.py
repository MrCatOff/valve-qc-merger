"""Explorer, Inspector and Log panels of the studio window."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.project import ASSET_KINDS, Project
from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import icon, pixmap
from valve_qc_merger.studio.model_info import ModelInfo, texture_rgba
from valve_qc_merger.studio.tasks import KIND_TITLES  # noqa: E402 - Qt-free, shared

ROLE_KIND = Qt.ItemDataRole.UserRole
ROLE_NAME = Qt.ItemDataRole.UserRole + 1
ROLE_CATEGORY = Qt.ItemDataRole.UserRole + 2  # the category a row sits in
UNCATEGORIZED = "Uncategorized"
NEW_CATEGORY = "\x00new"  # move_to_category target: ask for a new name

# status badge per AssetStatus.level
STATUS_COLORS = {"problem": theme.TOKENS["danger"], "stale": theme.TOKENS["warning"],
                 "ours": theme.TOKENS["success"], "own": theme.TOKENS["muted"]}
STATUS_GLYPHS = {"problem": "✕", "stale": "▲", "ours": "●", "own": "○"}
STATUS_HINTS = {"problem": "failed or rejected in its last build",
                "stale": "its source changed: re-run",
                "ours": "on our hands", "own": "own hands (not retargeted)"}
_ICONS: dict[str, QIcon] = {}


# Explorer quick filters: key -> (title, test(status, project, asset name))
FILTERS = {
    "own": ("Own hands (not retargeted)", lambda st, _p, _n: st is not None and st.hands == "own"),
    "ours": ("On our hands", lambda st, _p, _n: st is not None and st.hands == "ours"),
    "stale": ("Stale (source changed)", lambda st, _p, _n: st is not None and st.stale),
    "problem": ("Problems in builds", lambda st, _p, _n: st is not None and bool(st.problems)),
    "orphan": ("In no build", lambda st, _p, _n: st is not None and not st.builds),
    "derived": ("Made by Retarget",
                lambda _st, p, n: p is not None and bool(p.assets[n].derived)),
}


def status_icon(level: str) -> QIcon:
    """The level's badge — a shape as well as a colour, so it reads without
    colour vision: problem = circle with a cross, stale = triangle, on our
    hands = filled dot, own hands = ring (empty icon for "plain")."""
    if level not in _ICONS:
        icon_out = QIcon()
        for scale in (1, 2):
            pixmap = QPixmap(12 * scale, 12 * scale)
            pixmap.fill(Qt.GlobalColor.transparent)
            if level in STATUS_COLORS:
                painter = QPainter(pixmap)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.scale(scale, scale)
                colour = QColor(STATUS_COLORS[level])
                if level == "own":
                    painter.setPen(QPen(colour, 1.6))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawEllipse(QPointF(6, 6), 3.6, 3.6)
                elif level == "stale":
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(colour)
                    painter.drawPolygon([QPointF(6, 1.5), QPointF(10.8, 10), QPointF(1.2, 10)])
                else:
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(colour)
                    painter.drawEllipse(QPointF(6, 6), 4.6 if level == "problem" else 4.0,
                                        4.6 if level == "problem" else 4.0)
                    if level == "problem":
                        painter.setPen(QPen(QColor(theme.TOKENS["on_accent"]), 1.5))
                        painter.drawLine(QPointF(4.2, 4.2), QPointF(7.8, 7.8))
                        painter.drawLine(QPointF(7.8, 4.2), QPointF(4.2, 7.8))
                painter.end()
            pixmap.setDevicePixelRatio(scale)
            icon_out.addPixmap(pixmap)
        _ICONS[level] = icon_out
    return _ICONS[level]


# --------------------------------------------------------------------------- #
# Explorer
# --------------------------------------------------------------------------- #
class ExplorerPanel(QWidget):
    """The Explorer tree under a filter row (text + status)."""

    def __init__(self, explorer: Explorer, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.explorer = explorer
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter assets and builds (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.status_box = QComboBox()
        self.status_box.addItem("Any status", "")
        for key, (title, _test) in FILTERS.items():
            self.status_box.addItem(title, key)
        self.search.textChanged.connect(self._changed)
        self.status_box.currentIndexChanged.connect(self._changed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.search)
        layout.addWidget(self.status_box)
        layout.addWidget(explorer, 1)

    def _changed(self, *_args: object) -> None:
        self.explorer.set_filter(self.search.text(), self.status_box.currentData())

    def keyPressEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        if event.key() == Qt.Key.Key_Escape and self.explorer.filtering:
            self.search.clear()
            self.status_box.setCurrentIndex(0)
            return
        super().keyPressEvent(event)


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
    build_deploy_requested = Signal(str)
    derive_requested = Signal(list)  # asset names: open the Retarget dialog
    rederive_requested = Signal(str, bool)  # derived asset, edit settings first
    category_move_requested = Signal(list, str)  # assets, category ("" / NEW_CATEGORY)
    category_new_requested = Signal()
    category_rename_requested = Signal(str)
    category_delete_requested = Signal(str)
    category_builds_requested = Signal(str)
    sound_selected = Signal(str)  # a sound of the library (its sound/ path)
    sound_import_requested = Signal()
    sound_fix_requested = Signal(list)  # sound names
    sound_remove_requested = Signal(str)
    sound_play_requested = Signal(str)
    compare_requested = Signal(str, str)  # asset, the asset to compare it with
    sprite_selected = Signal(str)  # a sprite / HUD file (its sprites/ path)
    sprite_import_requested = Signal()
    sprite_new_requested = Signal()
    hud_new_requested = Signal()
    sprite_remove_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # drag assets onto a category (handled in dropEvent; nothing moves by itself)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setIndentation(20)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.currentItemChanged.connect(self._on_current)
        self._project: Project | None = None
        self.statuses: dict = {}  # asset -> project.status.AssetStatus
        self._filter_text = ""
        self._filter_level = ""  # "" or a FILTERS key

    @staticmethod
    def _item_key(item: QTreeWidgetItem | None) -> tuple:
        """A path that names ``item`` across rebuilds (kind + name, or the
        label without its count, for every level)."""
        parts = []
        while item is not None:
            name = item.data(0, ROLE_NAME)
            if name is None:
                name = re.split(r"  ·  | \(\d+\)$", item.text(0))[0]
            parts.append((item.data(0, ROLE_KIND), str(name)))
            item = item.parent()
        return tuple(reversed(parts))

    def _walk(self) -> list[QTreeWidgetItem]:
        out: list[QTreeWidgetItem] = []
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            item = stack.pop()
            out.append(item)
            stack += [item.child(i) for i in range(item.childCount())]
        return out

    def show_project(self, project: Project | None) -> None:
        """(Re)build the tree; the branches the user collapsed, the scroll
        position and the current row survive a rebuild of the same project."""
        same = (project is not None and self._project is not None
                and project.root == self._project.root and self.topLevelItemCount())
        collapsed = {self._item_key(i) for i in self._walk()
                     if i.childCount() and not i.isExpanded()} if same else set()
        current_key = self._item_key(self.currentItem()) if same else None
        scroll = self.verticalScrollBar().value() if same else 0
        self.blockSignals(True)
        try:
            self._show_project(project)
            if same:
                by_key = {self._item_key(i): i for i in self._walk()}
                for key in collapsed:
                    if key in by_key:
                        by_key[key].setExpanded(False)
                if current_key in by_key:
                    self.setCurrentItem(by_key[current_key])
        finally:
            self.blockSignals(False)
        if same:
            self.executeDelayedItemsLayout()
            self.verticalScrollBar().setValue(scroll)
        if not same or self._item_key(self.currentItem()) != current_key:
            self._on_current(self.currentItem(), None)

    def _show_project(self, project: Project | None) -> None:
        from valve_qc_merger.project.status import project_status
        self._project = project
        self.statuses = project_status(project) if project is not None else {}
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
        self._fill_sounds(project)
        self._fill_sprites(project)
        self.expandAll()
        self._apply_filter()

    def _fill_library(self, title: str, kind: str, names: list[str]) -> dict[str, QTreeWidgetItem]:
        """``title (N)`` with ``names`` as folders of files; returns the items."""
        root = QTreeWidgetItem(self, [f"{title} ({len(names)})"])
        root.setData(0, ROLE_KIND, f"{kind}s")
        folders: dict[str, QTreeWidgetItem] = {"": root}
        items: dict[str, QTreeWidgetItem] = {}
        for name in names:
            parent = root
            parts = name.split("/")
            for depth in range(1, len(parts)):
                key = "/".join(parts[:depth])
                if key not in folders:
                    folder = QTreeWidgetItem(folders["/".join(parts[:depth - 1])],
                                             [parts[depth - 1]])
                    folder.setData(0, ROLE_KIND, f"{kind}-folder")
                    folder.setData(0, ROLE_NAME, key)
                    folders[key] = folder
                parent = folders[key]
            item = QTreeWidgetItem(parent, [parts[-1]])
            item.setData(0, ROLE_KIND, kind)
            item.setData(0, ROLE_NAME, name)
            items[name] = item
        return items

    def _fill_sprites(self, project: Project) -> None:
        from valve_qc_merger.project.sprites import list_sprites
        self._fill_library("Sprites", "sprite", list_sprites(project))

    def _fill_sounds(self, project: Project) -> None:
        """Sounds (N): the library as folders of files, a badge on files the
        engine would mangle."""
        from valve_qc_merger.project.sounds import list_sounds, sound_path
        from valve_qc_merger.studio.sound_panel import sound_problems
        names = list_sounds(project)
        root = QTreeWidgetItem(self, [f"Sounds ({len(names)})"])
        root.setData(0, ROLE_KIND, "sounds")
        folders: dict[str, QTreeWidgetItem] = {"": root}
        for name in names:
            parent = root
            parts = name.split("/")
            for depth in range(1, len(parts)):
                key = "/".join(parts[:depth])
                if key not in folders:
                    folder = QTreeWidgetItem(folders["/".join(parts[:depth - 1])],
                                             [parts[depth - 1]])
                    folder.setData(0, ROLE_KIND, "sound-folder")
                    folder.setData(0, ROLE_NAME, key)
                    folders[key] = folder
                parent = folders[key]
            item = QTreeWidgetItem(parent, [parts[-1]])
            item.setData(0, ROLE_KIND, "sound")
            item.setData(0, ROLE_NAME, name)
            found = sound_problems(sound_path(project, name))
            if found:
                item.setIcon(0, status_icon("stale"))
                item.setToolTip(0, "\n".join(found))

    # -- tree connectors (├─ └─ │) -------------------------------------------
    def drawBranches(self, painter: QPainter, rect, index) -> None:  # noqa: ANN001, N802
        """Connector lines like a text tree plus a small chevron on expandable
        rows; top-level rows get only the chevron. Clicks still toggle (the
        view hit-tests the branch area itself)."""
        model = index.model()
        indent = self.indentation()
        chain = []  # index, its parent, ..., the top-level row
        current = index
        while current.isValid():
            chain.append(current)
            current = current.parent()
        depth = len(chain) - 1
        right = rect.right() + 1
        top, bottom = rect.top(), rect.bottom() + 1
        mid_y = (top + bottom) // 2

        def has_next(ix) -> bool:  # noqa: ANN001
            return ix.row() + 1 < model.rowCount(ix.parent())

        line = QColor(self.palette().text().color())
        line.setAlpha(110)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        pen = QPen(line)
        pen.setWidth(1)
        painter.setPen(pen)
        # depth 0 rows (Assets, Builds) are not connected to each other
        for level in range(1, depth + 1):
            x = right - (depth - level + 1) * indent + indent // 2
            node = chain[depth - level]  # the row's ancestor at this level
            if level == depth:  # the row's own column: ├─ or └─
                painter.drawLine(x, top, x, bottom if has_next(node) else mid_y)
                painter.drawLine(x, mid_y, right - 2, mid_y)
            elif has_next(node):  # an open ancestor branch passes by: │
                painter.drawLine(x, top, x, bottom)
        if model.hasChildren(index):
            x = right - indent // 2
            box = indent // 2 - 1
            painter.fillRect(x - box, mid_y - box, 2 * box, 2 * box,
                             self.palette().base())
            arrow = QPainterPath()
            r = 3.5
            if self.isExpanded(index):
                arrow.moveTo(x - r, mid_y - r / 2)
                arrow.lineTo(x, mid_y + r / 2)
                arrow.lineTo(x + r, mid_y - r / 2)
            else:
                arrow.moveTo(x - r / 2, mid_y - r)
                arrow.lineTo(x + r / 2, mid_y)
                arrow.lineTo(x - r / 2, mid_y + r)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            arrow_pen = QPen(self.palette().text().color())
            arrow_pen.setWidthF(1.5)
            painter.setPen(arrow_pen)
            painter.drawPath(arrow)
        painter.restore()

    # -- filter ----------------------------------------------------------------
    def set_filter(self, text: str = "", level: str = "") -> None:
        """Show only assets whose name contains ``text`` (case-insensitive)
        and whose status passes ``level`` (a :data:`FILTERS` key); their
        parents stay visible. Builds filter by name."""
        self._filter_text = text.strip().lower()
        self._filter_level = level
        self._apply_filter()

    @property
    def filtering(self) -> bool:
        return bool(self._filter_text or self._filter_level)

    def _asset_passes(self, name: str) -> bool:
        if self._filter_text and self._filter_text not in name.lower():
            return False
        entry = FILTERS.get(self._filter_level)
        return entry is None or entry[1](self.statuses.get(name), self._project, name)

    def _apply_filter(self) -> None:
        def walk(item: QTreeWidgetItem) -> bool:
            kind = item.data(0, ROLE_KIND)
            children = [walk(item.child(i)) for i in range(item.childCount())]
            if kind == "asset":
                visible = self._asset_passes(str(item.data(0, ROLE_NAME))) or any(children)
            elif kind in ("build", "sound", "sprite"):
                visible = (not self._filter_level and
                           self._filter_text in str(item.data(0, ROLE_NAME)).lower())
            elif kind in ("assets", "builds", "sounds", "sprites"):
                visible = True
            else:  # category / kind group: only if something inside shows
                visible = any(children) or not self.filtering
            item.setHidden(not visible)
            return visible
        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))
        if self.filtering:
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
            # a derived asset hangs under its source when the source is in the
            # same group; otherwise it stands alone, marked with its source
            shown = set(members)
            children: dict[str, list[str]] = {}
            for name in members:
                derived = project.assets[name].derived
                if derived and derived["from"] in shown and derived["from"] != name:
                    children.setdefault(derived["from"], []).append(name)
            nested = {n for kids in children.values() for n in kids}
            placed: set[str] = set()
            for name in members:
                if name not in nested:
                    self._add_asset(group, name, False, project, category, selected,
                                    children, placed)
            for name in members:  # unreachable from a root (a cycle): show flat
                if name not in placed:
                    self._add_asset(group, name, False, project, category, selected,
                                    children, placed)

    def _add_asset(self, holder: QTreeWidgetItem, name: str, nested: bool,
                   project: Project, category: str | None, selected: str,
                   children: dict[str, list[str]], placed: set[str]) -> None:
        placed.add(name)
        derived = project.assets[name].derived
        if nested:
            text = f"{name}  ·  {derived['mode']}"
        elif derived:
            text = f"{name}  ↳ {derived['from']}"
        else:
            text = name
        item = QTreeWidgetItem(holder, [text])
        item.setData(0, ROLE_KIND, "asset")
        item.setData(0, ROLE_NAME, name)
        item.setData(0, ROLE_CATEGORY, category)
        status = self.statuses.get(name)
        if status is not None:
            item.setIcon(0, status_icon(status.level))
            tip = [f"{derived['mode']} from {derived['from']}"] if derived else []
            item.setToolTip(0, "\n".join(tip + status.lines()))
        if name == selected:
            self.setCurrentItem(item)
        for child in children.get(name, []):
            if child not in placed:
                self._add_asset(item, child, True, project, category, selected,
                                children, placed)

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
        if kind == "sound":
            self.sound_selected.emit(str(item.data(0, ROLE_NAME)))
            return
        if kind == "sprite":
            self.sprite_selected.emit(str(item.data(0, ROLE_NAME)))
            return
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

    def assets_under(self, item: QTreeWidgetItem) -> list[str]:
        """Every asset at or below ``item``."""
        out: list[str] = []
        stack = [item]
        while stack:
            node = stack.pop()
            if node.data(0, ROLE_KIND) == "asset":
                out.append(str(node.data(0, ROLE_NAME)))
            stack += [node.child(i) for i in range(node.childCount())]
        return sorted(set(out))

    def retargetable(self, names: list[str]) -> list[str]:
        """The view models of ``names`` that Retarget would put on our hands:
        imported ones (not made by Retarget) without a swap-hands asset yet."""
        project = self._project
        if project is None:
            return []
        made = {a.derived["from"] for a in project.assets.values()
                if a.derived and a.derived.get("mode") == "hands"}
        return [n for n in names if n in project.assets and project.assets[n].kind == "v"
                and not project.assets[n].derived and n not in made]

    def current_sound(self) -> str:
        item = self.currentItem()
        if item is not None and item.data(0, ROLE_KIND) == "sound":
            return str(item.data(0, ROLE_NAME))
        return ""

    def sounds_under(self, item: QTreeWidgetItem) -> list[str]:
        if item.data(0, ROLE_KIND) == "sound":
            return [str(item.data(0, ROLE_NAME))]
        out: list[str] = []
        for i in range(item.childCount()):
            out += self.sounds_under(item.child(i))
        return out

    def _context_menu(self, pos) -> None:  # noqa: ANN001 - QPoint
        item = self.itemAt(pos)
        if item is not None and item.data(0, ROLE_KIND) in ("sprites", "sprite-folder",
                                                             "sprite"):
            menu = QMenu(self)
            if item.data(0, ROLE_KIND) == "sprite":
                name = str(item.data(0, ROLE_NAME))
                menu.addAction("Remove", lambda: self.sprite_remove_requested.emit(name))
                menu.addSeparator()
            menu.addAction("Import sprites…", self.sprite_import_requested.emit)
            menu.addAction("New sprite from images…", self.sprite_new_requested.emit)
            menu.addAction("New weapon HUD…", self.hud_new_requested.emit)
            menu.exec(self.viewport().mapToGlobal(pos))
            return
        if item is not None and item.data(0, ROLE_KIND) in ("sounds", "sound-folder",
                                                             "sound"):
            menu = QMenu(self)
            if item.data(0, ROLE_KIND) == "sound":
                name = str(item.data(0, ROLE_NAME))
                menu.addAction("Play", lambda: self.sound_play_requested.emit(name))
            names = self.sounds_under(item)
            if names:
                menu.addAction("Fix…" if len(names) == 1 else f"Fix {len(names)} sounds…",
                               lambda: self.sound_fix_requested.emit(names))
            if item.data(0, ROLE_KIND) == "sound":
                menu.addAction("Remove", lambda: self.sound_remove_requested.emit(
                    str(item.data(0, ROLE_NAME))))
            menu.addSeparator()
            menu.addAction("Import sounds…", self.sound_import_requested.emit)
            menu.exec(self.viewport().mapToGlobal(pos))
            return
        if item is not None and item.data(0, ROLE_KIND) == "group":
            names = self.retargetable(self.assets_under(item))
            if names:
                menu = QMenu(self)
                menu.addAction(f"Retarget {len(names)} view model(s)…",
                               lambda: self.derive_requested.emit(names))
                menu.exec(self.viewport().mapToGlobal(pos))
            return
        if item is not None and item.data(0, ROLE_KIND) in ("assets", "category"):
            menu = QMenu(self)
            names = self.retargetable(self.assets_under(item))
            if names:
                menu.addAction(f"Retarget {len(names)} view model(s)…",
                               lambda: self.derive_requested.emit(names))
                menu.addSeparator()
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
            menu.addAction("Deploy to game", lambda: self.build_deploy_requested.emit(name))
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
        if self._project is not None and len(self._project.assets) > 1:
            compare = menu.addMenu("Compare with")
            kind = self._project.assets[name].kind
            others = sorted((a for a in self._project.assets.values() if a.name != name),
                            key=lambda a: (a.kind != kind, a.name.lower()))
            for other in others[:40]:
                compare.addAction(other.name, lambda o=other.name:
                                  self.compare_requested.emit(name, o))
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

    def showEvent(self, event) -> None:  # noqa: ANN001, N802 - set while on a hidden tab
        super().showEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            super().clear()
            return
        # nearest-neighbour keeps the 8-bit texels crisp when enlarged; at the
        # screen's pixel ratio so a HiDPI display is not blurred
        ratio = self.devicePixelRatioF() or 1.0
        scaled = self._pixmap.scaled(
            int(self.width() * ratio), int(self.height() * ratio),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
        scaled.setDevicePixelRatio(ratio)
        self.setPixmap(scaled)


class NotesEdit(QPlainTextEdit):
    """A few lines of notes; ``editingFinished`` when focus leaves."""

    editingFinished = Signal()  # noqa: N815 - Qt naming

    def focusOutEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        super().focusOutEvent(event)
        self.editingFinished.emit()

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str) -> None:  # noqa: N802 - mirrors QLineEdit
        self.setPlainText(text)


class StatTile(QFrame):
    """A number with a caption (Overview's contents)."""

    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        theme.set_role(self, "tile")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(0)
        self.value = QLabel("—")
        theme.set_role(self.value, "tile-value")
        self.caption = QLabel(caption)
        theme.set_role(self.caption, "faint")
        layout.addWidget(self.value)
        layout.addWidget(self.caption)


def _section(title: str) -> QLabel:
    label = QLabel(title.upper())
    theme.set_role(label, "section")
    return label


def _stack(*parts: tuple[str, QWidget], stretch: tuple[int, ...] = ()) -> QSplitter:
    """Titled panes one above the other (a Geometry / Animation tab)."""
    splitter = QSplitter(Qt.Orientation.Vertical)
    splitter.setChildrenCollapsible(False)
    for index, (title, widget) in enumerate(parts):
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(8, 10, 8, 4)
        layout.setSpacing(6)
        layout.addWidget(_section(title))
        layout.addWidget(widget, 1)
        splitter.addWidget(pane)
        splitter.setStretchFactor(index, stretch[index] if index < len(stretch) else 1)
    return splitter


STAT_CAPTIONS = ("submodels", "triangles", "textures", "sequences", "bones", "attachments")


class Inspector(QWidget):
    """The selected asset: a header card (name, kind, status, actions) over
    tabs — Overview, Geometry (bodygroups, textures, skins), Animation
    (sequences, attachments), Bones, QC."""

    kind_changed = Signal(str, str)  # asset, kind
    notes_changed = Signal(str, str)
    hands_model_changed = Signal(str, bool)  # asset, the hands are the model
    sequence_edit_requested = Signal(int)  # sequence position in the QC
    render_mode_edit_requested = Signal(str)  # texture name
    retarget_requested = Signal(str)
    rederive_requested = Signal(str)
    reveal_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._asset = ""
        self._path = ""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages)
        self.empty_page = self._empty_page()
        self.pages.addWidget(self.empty_page)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self._header())
        self.tabs = QTabWidget()
        content_layout.addWidget(self.tabs, 1)
        self.pages.addWidget(content)

        # -- overview
        overview = QWidget()
        over = QVBoxLayout(overview)
        over.setContentsMargins(12, 12, 12, 12)
        over.setSpacing(10)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        self.kind_box = QComboBox()
        for kind in ASSET_KINDS:
            self.kind_box.addItem(KIND_TITLES[kind], kind)
        self.kind_box.activated.connect(self._kind_activated)
        self.category_label = QLabel("—")
        self.status_label = QLabel("—")
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_label = QLabel("—")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        copy = QToolButton()
        copy.setIcon(icon("copy"))
        copy.setToolTip("Copy the full folder path")
        copy.setAutoRaise(True)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self._path))
        reveal = QToolButton()
        reveal.setIcon(icon("folder-open"))
        reveal.setToolTip("Show the folder")
        reveal.setAutoRaise(True)
        reveal.clicked.connect(lambda: self._asset and self.reveal_requested.emit(self._asset))
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(2)
        path_row.addWidget(self.path_label, 1)
        path_row.addWidget(copy)
        path_row.addWidget(reveal)
        path_holder = QWidget()
        path_holder.setLayout(path_row)
        self.source_label = QLabel("—")
        self.source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # long names/paths (Windows paths cannot wrap at '\\') must never set
        # the dock's minimum width: the full text is in the tooltip
        for label in (self.category_label, self.status_label, self.path_label,
                      self.source_label):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            label.setWordWrap(True)
        form.addRow("Kind", self.kind_box)
        self.hands_model_box = QCheckBox("Hands are the model")
        self.hands_model_box.setToolTip(
            "Zombie claws, a gauntlet, or a view model with no hands at all: no retarget — "
            "merge-v builds merge it as it is into their no-hands part (<name>_nohands, "
            "pev_body = the model). Set on import when it is clear; tick it for the rest.")
        self.hands_model_box.toggled.connect(
            lambda on: self._asset and self.hands_model_changed.emit(self._asset, on))
        form.addRow("", self.hands_model_box)
        form.addRow("Category", self.category_label)
        form.addRow("Status", self.status_label)
        form.addRow("Folder", path_holder)
        form.addRow("Source", self.source_label)
        over.addLayout(form)
        over.addWidget(_section("Contents"))
        tiles = QGridLayout()
        tiles.setSpacing(6)
        self.stat_tiles: dict[str, StatTile] = {}
        for index, caption in enumerate(STAT_CAPTIONS):
            tile = StatTile(caption)
            self.stat_tiles[caption] = tile
            tiles.addWidget(tile, index // 3, index % 3)
        over.addLayout(tiles)
        self.warnings_label = QLabel("")
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setProperty("role", "warning")
        self.warnings_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                          QSizePolicy.Policy.Preferred)
        over.addWidget(self.warnings_label)
        over.addWidget(_section("Notes"))
        self.notes = NotesEdit()
        self.notes.setPlaceholderText("Anything worth remembering about this model…")
        self.notes.setFixedHeight(76)
        self.notes.editingFinished.connect(
            lambda: self._asset and self.notes_changed.emit(self._asset, self.notes.text()))
        over.addWidget(self.notes)
        over.addStretch(1)
        overview_scroll = QScrollArea()
        overview_scroll.setWidgetResizable(True)
        overview_scroll.setWidget(overview)
        overview_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tabs.addTab(overview_scroll, "Overview")

        # -- geometry: bodygroups, textures (+ preview), skins
        self.submodels = _table(["Bodygroup", "Submodel", "Triangles", "Vertices", "Textures"])
        textures = QWidget()
        tex_layout = QHBoxLayout(textures)
        tex_layout.setContentsMargins(0, 0, 0, 0)
        self.textures = _table(["Texture", "Size", "Render mode", "Used by"])
        # fit the pane: names and users take what is left (elided), no sideways scroll
        header = self.textures.horizontalHeader()
        header.setStretchLastSection(False)
        for column, mode in enumerate((QHeaderView.ResizeMode.Stretch,
                                       QHeaderView.ResizeMode.ResizeToContents,
                                       QHeaderView.ResizeMode.ResizeToContents,
                                       QHeaderView.ResizeMode.Stretch)):
            header.setSectionResizeMode(column, mode)
        self.textures.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.textures.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.textures.currentCellChanged.connect(self._show_texture)
        self.textures.setToolTip("Double-click the render mode to change it")
        self.textures.cellDoubleClicked.connect(self._texture_double_clicked)
        self.texture_preview = PixmapLabel()
        self.texture_preview.setFixedWidth(150)
        self.texture_preview.setMinimumHeight(120)
        self.texture_preview.setToolTip("The selected texture")
        tex_layout.addWidget(self.textures, 1)
        tex_layout.addWidget(self.texture_preview, 0, Qt.AlignmentFlag.AlignTop)
        from valve_qc_merger.studio.qc_tools import QcPage, SkinsPage
        self.skins_page = SkinsPage()
        self.geometry_tab = _stack(("Bodygroups", self.submodels), ("Textures", textures),
                                   ("Skins", self.skins_page), stretch=(2, 3, 1))
        self.tabs.addTab(self.geometry_tab, "Geometry")

        # -- animation: sequences, attachments
        self.sequences = _table(["#", "Sequence", "FPS", "Frames", "Loop", "Events"])
        self.sequences.setToolTip("Double-click: edit name, fps, loop, activity and events")
        self.sequences.cellDoubleClicked.connect(
            lambda row, _c: self._asset and self.sequence_edit_requested.emit(row))
        from valve_qc_merger.studio.bone_tools import AttachmentsPage, BonesPage
        self.attachments_page = AttachmentsPage()
        self.attachments = self.attachments_page.table
        self.animation_tab = _stack(("Sequences", self.sequences),
                                    ("Attachments", self.attachments_page), stretch=(3, 2))
        self.tabs.addTab(self.animation_tab, "Animation")

        self.bones_page = BonesPage()
        self.bones = self.bones_page.tree
        self.tabs.addTab(self.bones_page, "Bones")
        self.qc_page = QcPage()
        self.tabs.addTab(self.qc_page, "QC")
        for page in (self.bones_page, self.qc_page):  # breathing room, as the other tabs
            if page.layout() is not None:
                page.layout().setContentsMargins(8, 8, 8, 8)
        self._info: ModelInfo | None = None
        self.show_asset(None, None)

    # tabs API, so callers can treat the inspector as its tab widget
    def setCurrentWidget(self, widget: QWidget) -> None:  # noqa: N802 - Qt naming
        for tab in range(self.tabs.count()):
            page = self.tabs.widget(tab)
            if page is widget or page.isAncestorOf(widget):
                self.tabs.setCurrentIndex(tab)
                return

    def currentWidget(self) -> QWidget:  # noqa: N802 - Qt naming
        return self.tabs.currentWidget()

    def _empty_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        art = QLabel()
        art.setPixmap(pixmap("mouse-pointer-click", theme.TOKENS["faint"], 36,
                             scale=2.0, stroke=1.5))
        art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(art)
        text = QLabel("Select an asset in the Explorer")
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        theme.set_role(text, "hint")
        layout.addWidget(text)
        hint = QLabel("Its bodygroups, textures, sequences, bones and QC show up here.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        theme.set_role(hint, "faint")
        layout.addWidget(hint)
        layout.addStretch(2)
        return page

    def _header(self) -> QWidget:
        header = QWidget()
        theme.set_role(header, "header")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(header)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.name_label = QLabel("—")
        theme.set_role(self.name_label, "heading")
        self.name_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top.addWidget(self.name_label, 1)
        self.kind_badge = QLabel("")
        theme.set_role(self.kind_badge, "kbd")
        top.addWidget(self.kind_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(top)
        self.status_line = QLabel("")
        theme.set_role(self.status_line, "hint")
        self.status_line.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.status_line)
        actions = QHBoxLayout()
        actions.setSpacing(6)

        def action(text: str, name: str, tip: str, signal: Signal) -> QPushButton:
            button = QPushButton(icon(name), text)
            button.setToolTip(tip)
            button.clicked.connect(lambda: self._asset and signal.emit(self._asset))
            actions.addWidget(button)
            return button

        self.retarget_button = action("Retarget…", "hand", "Make a new asset from this one: "
                                      "swap hands, canonical bones or a zombie grenade (Ctrl+R)",
                                      self.retarget_requested)
        self.rederive_button = action("Re-run", "refresh-cw", "Re-run the retarget that made "
                                      "this asset, from its source (Ctrl+Shift+R)",
                                      self.rederive_requested)
        self.reveal_button = action("Folder", "folder-search", "Show the asset's folder",
                                    self.reveal_requested)
        actions.addStretch(1)
        layout.addLayout(actions)
        return header

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
            self.pages.setCurrentWidget(self.empty_page)
            self.name_label.setText("—")
            self.kind_badge.setText("")
            self.status_line.setText("")
            self.category_label.setText("—")
            self.status_label.setText("—")
            self.path_label.setText("—")
            self.source_label.setText("—")
            self._path = ""
            self._set_tiles(None)
            self.notes.setText("")
            self.warnings_label.setText("")
            for table in (self.submodels, self.textures, self.sequences):
                table.setRowCount(0)
            self.texture_preview.set_image(None)
            return
        self.pages.setCurrentIndex(1)
        self.name_label.setText(asset.name)
        self.name_label.setToolTip(asset.name)
        self.kind_badge.setText(KIND_TITLES.get(asset.kind, asset.kind))
        self.category_label.setText(asset.category or UNCATEGORIZED)
        self.status_label.setText("—")
        self.status_line.setText(f"{asset.category or UNCATEGORIZED}")
        self.kind_box.setCurrentIndex(ASSET_KINDS.index(asset.kind))
        self.hands_model_box.blockSignals(True)
        self.hands_model_box.setChecked(asset.hands_model)
        self.hands_model_box.blockSignals(False)
        self.hands_model_box.setVisible(asset.kind == "v")
        self._path = str(project.root / asset.path)
        self.path_label.setText(Path(asset.path).as_posix())
        self.path_label.setToolTip(self._path)
        derived = asset.derived
        self.rederive_button.setVisible(bool(derived))
        if derived:
            options = ", ".join(f"{k}={v}" for k, v in derived.get("options", {}).items())
            self.source_label.setText(
                f"{derived['mode']} from asset {derived['from']}"
                + (f" ({options})" if options else ""))
            self.source_label.setToolTip(self.source_label.text())
        else:
            self.source_label.setText(asset.source or "—")
            self.source_label.setToolTip(asset.source or "")
        self.notes.setText(asset.notes)
        if info is None:
            self._set_tiles(None)
            return
        self._set_tiles(info)
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

    def _set_tiles(self, info: ModelInfo | None) -> None:
        values = dict.fromkeys(STAT_CAPTIONS, "—") if info is None else {
            "submodels": len(info.submodels), "triangles": f"{info.triangles:,}",
            "textures": len(info.textures), "sequences": len(info.sequences),
            "bones": len(info.bones), "attachments": len(info.attachments)}
        for caption, tile in self.stat_tiles.items():
            tile.value.setText(str(values[caption]))

    def _texture_double_clicked(self, row: int, _column: int) -> None:
        item = self.textures.item(row, 0)
        if self._asset and item is not None:
            self.render_mode_edit_requested.emit(item.text())

    def show_status(self, status: object) -> None:
        """The asset's AssetStatus (from the Explorer's last refresh)."""
        category = self.category_label.text() if self._asset else ""
        if status is None:
            self.status_label.setText("—")
            self.status_line.setText(category)
            return
        color = STATUS_COLORS.get(status.level)
        lines = status.lines()
        glyph = STATUS_GLYPHS.get(status.level, "●")  # the badge's shape, not only colour
        dot = f"<span style='color:{color}'>{glyph}</span> " if color else ""
        self.status_label.setText("<br>".join([dot + lines[0]] + lines[1:]))
        self.status_line.setText(f"{dot}{lines[0]}  ·  {category}")

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
def project_title(project: Project | None, path: Path | None = None) -> str:
    if project is None:
        return "valve-qc-merger Studio"
    return f"{project.name} — valve-qc-merger Studio"


__all__ = ["Explorer", "Inspector", "KIND_TITLES", "project_title"]
