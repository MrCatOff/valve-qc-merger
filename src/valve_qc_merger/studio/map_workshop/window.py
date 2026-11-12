"""Project ▸ Map Workshop: a map from CSO made ready for a server, in one
window — every action is :mod:`valve_qc_merger.mapkit` run in the
background, every change goes into the map's plan first (undo / redo) and
nothing is written until Apply."""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QKeyEvent, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QGraphicsEllipseItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.mapkit import external as X
from valve_qc_merger.mapkit.analyze import MapReport, game_folder, map_wads
from valve_qc_merger.mapkit.fix import (
    FixOptions,
    Operation,
    plan_embed,
    plan_extract,
    plan_fixes,
    plan_replace_textures,
)
from valve_qc_merger.mapkit.hull import EMPTY, STAND, point_contents
from valve_qc_merger.mapkit.render import Plan, render_plan
from valve_qc_merger.mapkit.rules import known_class
from valve_qc_merger.mapkit.spawns import CT, SpawnOptions, T, generate
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import icon
from valve_qc_merger.studio.jobs import JobRunner
from valve_qc_merger.studio.map_workshop.state import MapState, WorkshopState
from valve_qc_merger.studio.viewport import Viewport

LEVEL_TOKEN = {"critical": "danger", "warning": "warning", "info": "muted", "ok": "success"}
LEVEL_GLYPH = {"critical": "✕", "warning": "▲", "info": "●", "ok": "✓"}
TEAM_COLOUR = {CT: QColor(80, 150, 255), T: QColor(255, 90, 80)}


def _cell(text: object, token: str | None = None, tip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(str(text))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if token:
        item.setForeground(theme.color(token))
    if tip:
        item.setToolTip(tip)
    return item


def _qimage(rgba: np.ndarray) -> QImage:
    data = np.ascontiguousarray(rgba)
    h, w = data.shape[:2]
    return QImage(data.tobytes(), w, h, w * 4, QImage.Format.Format_RGBA8888).copy()


def _origin(entity: dict[str, str]) -> tuple[float, float, float] | None:
    try:
        x, y, z = (float(v) for v in entity.get("origin", "").split()[:3])
    except ValueError:
        return None
    return x, y, z


# -- the plan (from above) ---------------------------------------------------------

class SpawnItem(QGraphicsEllipseItem):
    """A spawn on the plan: drag to move, right click for more."""

    def __init__(self, view: PlanView, index: int, entity: dict[str, str], radius: float
                 ) -> None:
        super().__init__(-radius, -radius, 2 * radius, 2 * radius)
        self.view, self.index, self.entity = view, index, entity
        colour = TEAM_COLOUR.get(entity.get("classname", ""), QColor(220, 220, 220))
        self.setBrush(QBrush(colour))
        self.setPen(QPen(QColor(20, 20, 20), 1.5))
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setZValue(10)
        self.setToolTip(f"#{index} {entity.get('classname')} at {entity.get('origin')}")

    def mark_stuck(self, stuck: bool) -> None:
        self.setPen(QPen(theme.color("danger") if stuck else QColor(20, 20, 20),
                         3.0 if stuck else 1.5))

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        super().mouseReleaseEvent(event)
        self.view.spawn_moved(self)


class PlanView(QGraphicsView):
    """The map from above with its (planned) spawns, editable."""

    changed = Signal(list)  # operations to add to the plan

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.plan: Plan | None = None
        self.state: MapState | None = None
        self.spawns: list[SpawnItem] = []
        self._fitted: Path | None = None

    def show_map(self, state: MapState, plan: Plan, entities: list[dict[str, str]]) -> None:
        self.state, self.plan = state, plan
        scene = self.scene()
        scene.clear()
        self.spawns = []
        scene.addItem(QGraphicsPixmapItem(QPixmap.fromImage(_qimage(plan.image))))
        radius = max(16 * plan.scale, 4.0)
        existing = len(state.entities())
        for index, entity in enumerate(entities):
            if entity.get("classname") not in (CT, T):
                continue
            origin = _origin(entity)
            if origin is None:
                continue
            item = SpawnItem(self, index if index < existing else -1, entity, radius)
            item.setPos(QPointF(*plan.to_pixel(origin[0], origin[1])))
            item.mark_stuck(not self._fits(origin))
            scene.addItem(item)
            self.spawns.append(item)
        scene.setSceneRect(QRectF(0, 0, plan.image.shape[1], plan.image.shape[0]))
        if self._fitted != state.path:  # a new map: the whole of it; edits keep the zoom
            self._fitted = state.path
            self.fitInView(scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def focus_on(self, origin: tuple[float, float, float],
                 others: list[tuple[float, float, float] | None] | None = None) -> None:
        """Centre the plan on ``origin`` and ring it (and ``others``) in yellow."""
        if self.plan is None:
            return
        pen = QPen(QColor(255, 210, 40), 3.0)
        radius = max(24 * self.plan.scale, 8.0)
        for point in [origin, *(others or [])]:
            if point is None:
                continue
            x, y = self.plan.to_pixel(point[0], point[1])
            ring = self.scene().addEllipse(x - radius, y - radius, 2 * radius, 2 * radius, pen)
            ring.setZValue(20)
        x, y = self.plan.to_pixel(origin[0], origin[1])
        self.resetTransform()
        self.scale(2.0, 2.0)
        self.centerOn(x, y)

    def _fits(self, origin: tuple[float, float, float]) -> bool:
        if self.state is None:
            return True
        point = np.array([[origin[0], origin[1], origin[2] + 1.0]])
        return bool(point_contents(self.state.bsp, point, STAND)[0] == EMPTY)

    def _world(self, pos: QPointF, z_hint: float) -> tuple[float, float, float]:
        assert self.plan is not None
        x, y = self.plan.to_world(pos.x(), pos.y())
        floor = self.plan.floor_at(x, y)
        z = floor + 37.0 if floor is not None else z_hint
        return round(x, 1), round(y, 1), round(z, 1)

    def spawn_moved(self, item: SpawnItem) -> None:
        if self.plan is None or item.index < 0:
            return
        old = _origin(item.entity)
        if old is None:
            return
        new = self._world(item.pos(), old[2])
        if math.dist(old[:2], new[:2]) < 1.0:
            return
        item.mark_stuck(not self._fits(new))
        self.changed.emit([Operation("spawn.fix", f"move spawn #{item.index} to "
                                     f"{new[0]:g} {new[1]:g} {new[2]:g}", item.index,
                                     {"origin": new})])

    def wheelEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        under = self.itemAt(event.position().toPoint())
        if (event.button() == Qt.MouseButton.LeftButton and self.plan is not None
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier
                and (under is None or isinstance(under, QGraphicsPixmapItem))):
            pos = self.mapToScene(event.position().toPoint())
            team = T if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else CT
            x, y, z = self._world(pos, 37.0)
            entity = {"classname": team, "origin": f"{x:g} {y:g} {z:g}", "angles": "0 0 0"}
            self.changed.emit([Operation("entity.add", f"add a {'T' if team == T else 'CT'} "
                                         f"spawn at {entity['origin']}", None,
                                         {"new": entity})])
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        item = self.itemAt(event.pos())
        if not isinstance(item, SpawnItem) or item.index < 0:
            return
        menu = QMenu(self)
        remove = menu.addAction("Remove this spawn")
        turn = menu.addAction("Turn 45°")
        chosen = menu.exec(event.globalPos())
        if chosen is remove:
            self.changed.emit([Operation("entity.remove", f"remove spawn #{item.index}",
                                         item.index)])
        elif chosen is turn:
            parts = item.entity.get("angles", "0 0 0").split() + ["0", "0", "0"]
            yaw = (float(parts[1]) + 45) % 360
            self.changed.emit([Operation("entity.set", f"#{item.index} angles: yaw {yaw:g}",
                                         item.index, {"key": "angles",
                                                      "value": f"0 {yaw:g} 0"})])


class MapViewport(Viewport):
    """The 3D view: orbit / pan / zoom with the mouse, fly with W A S D
    (Q / E down / up, Shift faster)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        camera = self.state.camera
        step = max(camera.distance * 0.05, 16.0) * (4 if event.modifiers()
                                                    & Qt.KeyboardModifier.ShiftModifier else 1)
        yaw = math.radians(camera.yaw)
        forward = (-math.cos(yaw), -math.sin(yaw))
        right = (-math.sin(yaw), math.cos(yaw))
        moves = {Qt.Key.Key_W: (forward[0], forward[1], 0.0),
                 Qt.Key.Key_S: (-forward[0], -forward[1], 0.0),
                 Qt.Key.Key_A: (-right[0], -right[1], 0.0),
                 Qt.Key.Key_D: (right[0], right[1], 0.0),
                 Qt.Key.Key_Q: (0.0, 0.0, -1.0), Qt.Key.Key_E: (0.0, 0.0, 1.0)}
        move = moves.get(Qt.Key(event.key()))
        if move is None:
            super().keyPressEvent(event)
            return
        tx, ty, tz = camera.target
        camera.target = (tx + move[0] * step, ty + move[1] * step, tz + move[2] * step)
        self.update()


# -- dialogs -----------------------------------------------------------------------

class SpawnDialog(QDialog):
    """Autospawns: how many a team, spread, distance."""

    def __init__(self, per_team: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Autospawns")
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header("Autospawns", "Spawns on floors players reach from the "
                                    "map's own (walking, through water, up ladders), apart, "
                                    "facing open space. The same map always gets the same."))
        form = kit.form()
        self.count = kit.number_spin(QSpinBox())
        self.count.setRange(1, 128)
        self.count.setValue(per_team)
        self.spread = QComboBox()
        self.spread.addItems(["uniform", "near_existing"])
        self.distance = kit.number_spin(QDoubleSpinBox())
        self.distance.setRange(32, 1024)
        self.distance.setValue(64)
        form.addRow("Spawns a team", self.count)
        form.addRow("Where", self.spread)
        form.addRow("At least apart (units)", self.distance)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def options(self) -> SpawnOptions:
        return SpawnOptions(per_team=self.count.value(), spread=self.spread.currentText(),
                            min_distance=self.distance.value())


class ImportDialog(QDialog):
    """Map ▸ Import: maps by name from unpacked CSO folders into a game folder."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import maps")
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header("Import maps", "From folders already unpacked (a CSO "
                                    "client, a map pack): each map with its WADs, sky, models, "
                                    "sprites and sounds, names in lower case, v9 models made "
                                    "v10. Files the base game has are not copied."))
        form = kit.form()
        self.source = QLineEdit()
        self.source.setPlaceholderText("unpacked client or pack folder")
        pick = QPushButton(icon("folder-open"), "")
        pick.clicked.connect(lambda: self._pick(self.source))
        row = QHBoxLayout()
        row.addWidget(self.source, 1)
        row.addWidget(pick)
        self.out = QLineEdit()
        self.out.setPlaceholderText("the game folder to import into (cstrike)")
        pick_out = QPushButton(icon("folder-open"), "")
        pick_out.clicked.connect(lambda: self._pick(self.out))
        row_out = QHBoxLayout()
        row_out.addWidget(self.out, 1)
        row_out.addWidget(pick_out)
        self.names = QLineEdit()
        self.names.setPlaceholderText("zm_forbidden zm_shoreline … (empty: every map)")
        form.addRow("Source", row)
        form.addRow("Into", row_out)
        form.addRow("Maps", self.names)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _pick(self, edit: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder", edit.text())
        if folder:
            edit.setText(folder)


# -- the window --------------------------------------------------------------------

class MapWorkshop(QDialog):
    """Overview, Plan, 3D, Entities, Textures, Props and Build of open maps."""

    def __init__(self, folders: list[Path] | None = None, parent: QWidget | None = None,
                 maps_folder: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Map Workshop")
        self.state = WorkshopState(folders=list(folders or []))
        self.jobs = JobRunner(self)
        self.jobs.log.connect(self._log)
        self.jobs.done.connect(self._job_done)
        self._work = Path(tempfile.mkdtemp(prefix="vqm_workshop_"))
        self._plan_image: Plan | None = None
        self._pending: dict[str, object] = {}
        self._prop_status: dict[str, str] = {}
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header(
            "Map Workshop", "Check a map, fix its entities and spawns, replace textures, "
            "merge its props, import maps from CSO folders. Changes go into the plan below; "
            "Apply writes them (the CRC is kept when only entities change)."))
        split = QSplitter()
        layout.addWidget(split, 1)
        split.addWidget(self._map_list())
        self.tabs = QTabWidget()
        split.addWidget(self.tabs)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 900])
        self.tabs.addTab(self._overview_tab(), icon("layout-dashboard"), "Overview")
        self.tabs.addTab(self._plan_tab(), icon("map"), "Plan")
        self.tabs.addTab(self._view3d_tab(), icon("rotate-3d"), "3D")
        self.tabs.addTab(self._entities_tab(), icon("list-checks"), "Entities")
        self.tabs.addTab(self._textures_tab(), icon("image"), "Textures")
        self.tabs.addTab(self._props_tab(), icon("boxes"), "Props")
        self.tabs.addTab(self._build_tab(), icon("hammer"), "Build")
        self.tabs.currentChanged.connect(lambda _i: self._show_tab())
        layout.addLayout(self._plan_bar())
        self.status = kit.hint("")
        layout.addWidget(self.status)
        self.resize(1240, 860)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        if maps_folder is not None and maps_folder.is_dir():
            for path in sorted(maps_folder.glob("*.bsp"))[:64]:
                self.open_map(path, select=False)
        self._refresh_all()

    # -- maps --------------------------------------------------------------------

    def _map_list(self) -> QWidget:
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(kit.section("Maps"))
        self.map_list = QListWidget()
        self.map_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.map_list.currentItemChanged.connect(lambda *_a: self._refresh_all())
        column.addWidget(self.map_list, 1)
        for text, name, slot in (("Open BSP…", "file-plus", self._open_files),
                                 ("Add folder…", "folder-plus", self._open_folder),
                                 ("Import…", "folder-input", self._import)):
            button = QPushButton(icon(name), text)
            button.clicked.connect(slot)
            column.addWidget(button)
        build = QPushButton(icon("wand-sparkles"), "Build selected (profile)")
        build.setToolTip("Check → fix → props → .res → check every selected map")
        build.clicked.connect(self._build_selected)
        column.addWidget(build)
        return box

    def open_map(self, path: Path, select: bool = True) -> MapState:
        state = self.state.open(path)
        existing = self._item_for(state)
        if existing is None:
            existing = QListWidgetItem()
            existing.setData(Qt.ItemDataRole.UserRole, str(state.path))
            self.map_list.addItem(existing)
        self._label_item(existing, state)
        if select:
            self.map_list.setCurrentItem(existing)
        return state

    def _item_for(self, state: MapState) -> QListWidgetItem | None:
        for row in range(self.map_list.count()):
            item = self.map_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == str(state.path):
                return item
        return None

    def _label_item(self, item: QListWidgetItem, state: MapState) -> None:
        level = state.report.level if state.report else "critical"
        pending = f"  ({len(state.plan.active)})" if state.plan.active else ""
        item.setText(f"{LEVEL_GLYPH.get(level, '?')}  {state.name}{pending}")
        item.setForeground(theme.color(LEVEL_TOKEN.get(level, "muted")))
        item.setToolTip(state.error or str(state.path))

    def current(self) -> MapState | None:
        item = self.map_list.currentItem()
        if item is None:
            return None
        return self.state.maps.get(Path(item.data(Qt.ItemDataRole.UserRole)))

    def selected(self) -> list[MapState]:
        out = [self.state.maps.get(Path(i.data(Qt.ItemDataRole.UserRole)))
               for i in self.map_list.selectedItems()]
        return [s for s in out if s is not None] or ([self.current()] if self.current() else [])

    def _open_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Open maps", "", "Maps (*.bsp)")
        for path in paths:
            self.open_map(Path(path))

    def _open_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "A folder of maps")
        if folder:
            for path in sorted(Path(folder).rglob("*.bsp")):
                self.open_map(path, select=False)
            self._refresh_all()

    # -- overview --------------------------------------------------------------------

    def _overview_tab(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        self.cards = QGridLayout()
        self.card_labels: dict[str, QLabel] = {}
        for i, key in enumerate(("entities", "faces", "textures", "brush models", "model slots",
                                 "sounds", "spawns CT", "spawns T")):
            title = kit.hint(key)
            value = QLabel("–")
            value.setStyleSheet("font-size: 18px; font-weight: 600;")
            self.cards.addWidget(title, (i // 4) * 2, i % 4)
            self.cards.addWidget(value, (i // 4) * 2 + 1, i % 4)
            self.card_labels[key] = value
        column.addLayout(self.cards)
        self.issues = QTableWidget(0, 4)
        self.issues.setHorizontalHeaderLabels(["Level", "Issue", "What", "Fix"])
        self.issues.verticalHeader().setVisible(False)
        self.issues.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.issues.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.issues.cellDoubleClicked.connect(self._reveal_issue)
        column.addWidget(self.issues, 1)
        row = QHBoxLayout()
        for text, name, slot, primary in (
                ("Fix selected", "wand-sparkles", lambda: self._fix(False), False),
                ("Fix all", "wand-sparkles", lambda: self._fix(True), True),
                ("Check again", "refresh-cw", self._recheck, False),
                ("Export report…", "file-down", self._export_report, False)):
            button = QPushButton(icon(name), text)
            button.clicked.connect(slot)
            if primary:
                theme.set_primary(button)
            row.addWidget(button)
        row.addStretch(1)
        column.addLayout(row)
        return page

    def _show_overview(self, state: MapState) -> None:
        report = state.report
        self.issues.setRowCount(0)
        if report is None:
            for value in self.card_labels.values():
                value.setText("–")
            self.issues.setRowCount(1)
            self.issues.setItem(0, 2, _cell(state.error or "not read", "danger"))
            return
        counts, slots, spawns = report.counts, report.slots, report.spawns
        values = {"entities": counts.get("entities"), "faces": counts.get("faces"),
                  "textures": counts.get("textures"), "brush models": counts.get("brush_models"),
                  "model slots": f"{slots.get('total')} (free {slots.get('free_models')})",
                  "sounds": slots.get("sounds"), "spawns CT": spawns.get("ct"),
                  "spawns T": spawns.get("t")}
        for key, value in values.items():
            self.card_labels[key].setText(str(value))
        issues = [i for i in report.issues if i.level != "info"] + \
            [i for i in report.issues if i.level == "info"]
        self.issues.setRowCount(len(issues))
        for row, issue in enumerate(issues):
            token = LEVEL_TOKEN[issue.level]
            self.issues.setItem(row, 0, _cell(f"{LEVEL_GLYPH[issue.level]} {issue.level}", token))
            self.issues.setItem(row, 1, _cell(issue.code))
            self.issues.setItem(row, 2, _cell(issue.text, tip=json.dumps(issue.where)[:400]))
            self.issues.setItem(row, 3, _cell(issue.fix or "—", "muted"))
            self.issues.item(row, 0).setData(Qt.ItemDataRole.UserRole, row)
        self._issues = issues

    def _fix(self, everything: bool) -> None:
        state = self.current()
        if state is None or state.report is None:
            return
        rows = {i.row() for i in self.issues.selectedIndexes()}
        chosen = self._issues if everything else [self._issues[r] for r in sorted(rows)]
        if not chosen:
            return
        report = MapReport(**{**state.report.__dict__, "issues": chosen})
        codes = {i.code for i in chosen}
        spawns = None if "few_spawns" in codes else 0
        options = FixOptions(spawns=spawns, wads="unused_wad" in codes or everything,
                             remove_missing=True)
        plan = plan_fixes(report, options, self.state.rules)
        state.add(plan.operations)
        self._notes(plan.notes)
        self._refresh_all()

    def _reveal_issue(self, row: int, _column: int) -> None:
        issue = self._issues[row] if row < len(getattr(self, "_issues", [])) else None
        if issue is None:
            return
        entities = issue.where.get("entities")
        state = self.current()
        if isinstance(entities, list) and entities and state is not None:
            listed = state.entities()
            origin = _origin(listed[int(entities[0])])
            if origin is not None:
                self.tabs.setCurrentIndex(1)
                self.plan_view.focus_on(origin, [_origin(listed[int(i)]) for i in entities])
            else:
                self.tabs.setCurrentIndex(3)
                self._select_entity(int(entities[0]))
        elif "texture" in issue.where or "textures" in issue.where:
            self.tabs.setCurrentIndex(4)

    def _recheck(self) -> None:
        state = self.current()
        if state is not None:
            self.state.check(state)
            self._refresh_all()

    def _export_report(self) -> None:
        state = self.current()
        if state is None or state.report is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export the report",
                                              f"{state.name}.json", "JSON (*.json)")
        if path:
            Path(path).write_text(json.dumps(state.report.to_dict(), indent=2,
                                             ensure_ascii=False), encoding="utf-8")

    # -- plan view -------------------------------------------------------------------

    def _plan_tab(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        row = QHBoxLayout()
        self.zmin = kit.number_spin(QSpinBox())
        self.zmax = kit.number_spin(QSpinBox())
        for spin, value in ((self.zmin, -4096), (self.zmax, 4096)):
            spin.setRange(-4096, 4096)
            spin.setValue(value)
            spin.setSingleStep(64)
            spin.editingFinished.connect(self._show_tab)
        row.addWidget(QLabel("Floors from"))
        row.addWidget(self.zmin)
        row.addWidget(QLabel("to"))
        row.addWidget(self.zmax)
        auto = QPushButton(icon("wand-sparkles"), "Autospawns…")
        auto.clicked.connect(self._autospawns)
        row.addWidget(auto)
        row.addStretch(1)
        row.addWidget(kit.hint("Drag a spawn to move it · right click: remove / turn · "
                               "Ctrl+click: add CT, Ctrl+Shift+click: add T · red ring: a "
                               "player does not fit"))
        column.addLayout(row)
        self.plan_view = PlanView()
        self.plan_view.changed.connect(self._add_ops)
        column.addWidget(self.plan_view, 1)
        return page

    def _show_plan(self, state: MapState) -> None:
        entities = state.planned_entities()
        wads = map_wads(state.path, state.bsp, self.state.folders)
        zmin = self.zmin.value() if self.zmin.value() > -4096 else None
        zmax = self.zmax.value() if self.zmax.value() < 4096 else None
        plan = render_plan(state.bsp, size=1400, wads=wads, entities=entities, spawns=False,
                           zmin=zmin, zmax=zmax)
        self.plan_view.show_map(state, plan, entities)

    def _autospawns(self) -> None:
        state = self.current()
        if state is None:
            return
        dialog = SpawnDialog(self.state.rules.min_spawns, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        options = dialog.options()
        entities = state.planned_entities()

        def work(reporter):  # noqa: ANN001, ANN202
            reporter.log(f"{state.name}: placing spawns…")
            return generate(state.bsp, entities, options)

        self._pending["spawns"] = state
        self.jobs.start("Autospawns", work)

    # -- 3D --------------------------------------------------------------------------

    def _view3d_tab(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        row = QHBoxLayout()
        load = QPushButton(icon("rotate-3d"), "Show in 3D")
        load.clicked.connect(self._show_3d)
        self.show_spawns = QCheckBox("Spawns")
        self.show_spawns.setChecked(True)
        row.addWidget(load)
        row.addWidget(self.show_spawns)
        row.addStretch(1)
        row.addWidget(kit.hint("Left drag: orbit · right drag: pan · wheel: zoom · W A S D, "
                               "Q / E: fly (Shift: faster) · double click: whole map"))
        column.addLayout(row)
        self.viewport = MapViewport()
        self.viewport.state.show_grid = False
        column.addWidget(self.viewport, 1)
        return page

    def _show_3d(self) -> None:
        from valve_qc_merger.studio.map_workshop.scene3d import map_scene
        state = self.current()
        if state is None:
            return
        wads = map_wads(state.path, state.bsp, self.state.folders)
        scene = map_scene(state.bsp, wads, state.planned_entities(),
                          self._work / state.name, self.show_spawns.isChecked())
        self.viewport.set_scene(scene)
        self.viewport.state.camera.pitch = 40.0
        self.viewport.setFocus()

    # -- entities --------------------------------------------------------------------

    def _entities_tab(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        row = QHBoxLayout()
        self.class_filter = QComboBox()
        self.class_filter.currentIndexChanged.connect(lambda _i: self._fill_entities())
        self.entity_search = QLineEdit()
        self.entity_search.setPlaceholderText("search keys and values")
        self.entity_search.textChanged.connect(lambda _t: self._fill_entities())
        row.addWidget(self.class_filter)
        row.addWidget(self.entity_search, 1)
        column.addLayout(row)
        split = QSplitter(Qt.Orientation.Vertical)
        self.entity_table = QTableWidget(0, 4)
        self.entity_table.setHorizontalHeaderLabels(["#", "Class", "Name", "Model / origin"])
        self.entity_table.verticalHeader().setVisible(False)
        self.entity_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)
        self.entity_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.entity_table.currentCellChanged.connect(lambda *_a: self._fill_keys())
        split.addWidget(self.entity_table)
        keys = QWidget()
        keys_column = QVBoxLayout(keys)
        keys_column.setContentsMargins(0, 0, 0, 0)
        self.key_table = QTableWidget(0, 2)
        self.key_table.setHorizontalHeaderLabels(["Key", "Value"])
        self.key_table.verticalHeader().setVisible(False)
        self.key_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.key_table.itemChanged.connect(self._key_edited)
        keys_column.addWidget(self.key_table, 1)
        buttons = QHBoxLayout()
        for text, name, slot in (("Add key", "file-plus", self._add_key),
                                 ("Remove key", "minus", self._remove_key),
                                 ("Remove entity", "trash-2", self._remove_entity)):
            button = QPushButton(icon(name), text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        keys_column.addLayout(buttons)
        split.addWidget(keys)
        column.addWidget(split, 1)
        return page

    def _show_entities(self, state: MapState) -> None:
        classes = sorted({e.get("classname", "") for e in state.entities()})
        current = self.class_filter.currentText()
        self.class_filter.blockSignals(True)
        self.class_filter.clear()
        self.class_filter.addItem("every class")
        self.class_filter.addItems(classes)
        if current in classes:
            self.class_filter.setCurrentText(current)
        self.class_filter.blockSignals(False)
        self._fill_entities()

    def _fill_entities(self) -> None:
        state = self.current()
        self.entity_table.setRowCount(0)
        if state is None:
            return
        cls = self.class_filter.currentText()
        query = self.entity_search.text().strip().lower()
        missing = set()
        if state.report is not None:
            for issue in state.report.issues:
                if issue.code in ("missing_model", "missing_sound"):
                    listed = issue.where.get("entities", [])
                    missing |= set(listed) if isinstance(listed, list) else set()
        rows = []
        for index, entity in enumerate(state.entities()):
            if cls != "every class" and entity.get("classname") != cls:
                continue
            if query and not any(query in f"{k} {v}".lower() for k, v in entity.items()):
                continue
            rows.append((index, entity))
        self.entity_table.setRowCount(len(rows))
        for row, (index, entity) in enumerate(rows):
            name = entity.get("classname", "")
            token = "danger" if index in missing else None if known_class(name) else "warning"
            self.entity_table.setItem(row, 0, _cell(index))
            self.entity_table.setItem(row, 1, _cell(name, token))
            self.entity_table.setItem(row, 2, _cell(entity.get("targetname", "")))
            self.entity_table.setItem(row, 3, _cell(entity.get("model")
                                                    or entity.get("origin", "")))

    def _entity_index(self) -> int | None:
        row = self.entity_table.currentRow()
        item = self.entity_table.item(row, 0) if row >= 0 else None
        return int(item.text()) if item is not None else None

    def _select_entity(self, index: int) -> None:
        self.class_filter.setCurrentIndex(0)
        self.entity_search.clear()
        self._fill_entities()
        for row in range(self.entity_table.rowCount()):
            if self.entity_table.item(row, 0).text() == str(index):
                self.entity_table.selectRow(row)
                return

    def _fill_keys(self) -> None:
        state, index = self.current(), self._entity_index()
        self.key_table.blockSignals(True)
        self.key_table.setRowCount(0)
        if state is not None and index is not None:
            entity = state.entities()[index]
            planned = dict(entity)
            for op in state.plan.active:
                if op.entity == index and op.code == "entity.set":
                    planned[str(op.data["key"])] = str(op.data["value"])
            self.key_table.setRowCount(len(planned))
            for row, (key, value) in enumerate(planned.items()):
                key_item = _cell(key)
                value_item = QTableWidgetItem(value)
                if entity.get(key) != value:
                    value_item.setForeground(theme.color("accent"))
                self.key_table.setItem(row, 0, key_item)
                self.key_table.setItem(row, 1, value_item)
        self.key_table.blockSignals(False)

    def _key_edited(self, item: QTableWidgetItem) -> None:
        state, index = self.current(), self._entity_index()
        if state is None or index is None or item.column() != 1:
            return
        key = self.key_table.item(item.row(), 0).text()
        state.add([Operation("entity.set", f"#{index} {key}: {item.text()}", index,
                             {"key": key, "value": item.text()})])
        self._refresh_plan_bar()

    def _add_key(self) -> None:
        state, index = self.current(), self._entity_index()
        if state is None or index is None:
            return
        key, ok = QInputDialog.getText(self, "Add a key", "Key")
        if ok and key.strip():
            value, ok = QInputDialog.getText(self, "Add a key", f"Value of {key}")
            if ok:
                state.add([Operation("entity.set", f"#{index} {key}: {value}", index,
                                     {"key": key.strip(), "value": value})])
                self._refresh_all()

    def _remove_key(self) -> None:
        state, index = self.current(), self._entity_index()
        row = self.key_table.currentRow()
        if state is None or index is None or row < 0:
            return
        key = self.key_table.item(row, 0).text()
        state.add([Operation("entity.unset", f"#{index} - {key}", index, {"key": key})])
        self._refresh_all()

    def _remove_entity(self) -> None:
        state = self.current()
        if state is None:
            return
        rows = sorted({i.row() for i in self.entity_table.selectedIndexes()})
        ops = [Operation("entity.remove", f"remove #{self.entity_table.item(r, 0).text()} "
                         f"{self.entity_table.item(r, 1).text()}",
                         int(self.entity_table.item(r, 0).text())) for r in rows]
        state.add(ops)
        self._refresh_all()

    # -- textures --------------------------------------------------------------------

    def _textures_tab(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        row = QHBoxLayout()
        self.texture_filter = QComboBox()
        self.texture_filter.addItems(["every texture", "embedded", "external", "missing",
                                      "over 256"])
        self.texture_filter.currentIndexChanged.connect(lambda _i: self._show_tab())
        row.addWidget(self.texture_filter)
        row.addStretch(1)
        column.addLayout(row)
        self.texture_list = TextureList()
        self.texture_list.dropped.connect(self._texture_dropped)
        self.texture_list.currentItemChanged.connect(lambda *_a: self._texture_selected())
        column.addWidget(self.texture_list, 1)
        self.texture_info = kit.hint("Drop a PNG / TGA / BMP on a texture to replace it "
                                     "(resized to its size; name_0.png … for an animation).")
        compare = QHBoxLayout()
        self.texture_before = QLabel()
        self.texture_after = QLabel()
        for label in (self.texture_before, self.texture_after):
            label.setFixedSize(160, 160)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet(f"border: 1px solid {theme.color('border').name()};")
        compare.addWidget(kit.hint("Before"))
        compare.addWidget(self.texture_before)
        compare.addWidget(kit.hint("After"))
        compare.addWidget(self.texture_after)
        compare.addWidget(self.texture_info, 1)
        column.addLayout(compare)
        buttons = QHBoxLayout()
        for text, name, slot in (("Replace…", "image-plus", self._replace_texture),
                                 ("Embed all", "package-plus", lambda: self._embed(False)),
                                 ("Embed non-stock", "package-plus", lambda: self._embed(True)),
                                 ("Extract to WAD…", "file-down", self._extract),
                                 ("Prune WADs", "eraser", self._prune)):
            button = QPushButton(icon(name), text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        column.addLayout(buttons)
        return page

    def _show_textures(self, state: MapState) -> None:
        from valve_qc_merger.mapkit.textures import lump_entries, texture_uses
        from valve_qc_merger.sprite.wad import MIPTEX, read_miptex
        wads = map_wads(state.path, state.bsp, self.state.folders)
        uses = texture_uses(state.bsp, {w.path.name: w for w in wads})
        mode = self.texture_filter.currentText()
        uses = [u for u in uses if mode == "every texture"
                or (mode == "embedded" and u.embedded) or (mode == "external" and not u.embedded)
                or (mode == "missing" and not u.found)
                or (mode == "over 256" and max(u.width, u.height) > 256)]
        entries = lump_entries(state.bsp)
        replaced = {str(op.data["name"]).lower(): op.data["texture"] for op in state.plan.active
                    if op.code in ("textures.replace", "textures.wad_copy")}
        self.texture_list.clear()
        for use in uses:
            raw = entries[use.index] if use.embedded else next(
                (lump for w in wads if (lump := w.lump(use.name)) is not None), None)
            item = QListWidgetItem(f"{use.name}\n{use.width}x{use.height}")
            item.setData(Qt.ItemDataRole.UserRole, use.name)
            texture = replaced.get(use.name.lower())
            try:
                texture = texture or (read_miptex(raw, use.name, MIPTEX) if raw else None)
            except (ValueError, IndexError):
                texture = None
            if texture is not None:
                pixmap = QPixmap.fromImage(_qimage(texture.rgba())).scaled(
                    96, 96, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                item.setIcon(QPixmap(pixmap))
            if not use.found:
                item.setForeground(theme.color("danger"))
            elif use.name.lower() in replaced:
                item.setForeground(theme.color("accent"))
            item.setToolTip(f"{use.name} {use.width}x{use.height} — "
                            f"{'embedded' if use.embedded else use.wad or 'MISSING'}, "
                            f"{use.faces} faces")
            self.texture_list.addItem(item)

    def _texture_selected(self) -> None:
        item = self.texture_list.currentItem()
        self.texture_before.clear()
        self.texture_after.clear()
        if item is None:
            return
        self.texture_info.setText(item.toolTip())
        before, after = self._texture_versions(str(item.data(Qt.ItemDataRole.UserRole)))
        for label, texture in ((self.texture_before, before), (self.texture_after, after)):
            if texture is not None:
                label.setPixmap(QPixmap.fromImage(_qimage(texture.rgba())).scaled(
                    156, 156, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
            else:
                label.setText("—" if label is self.texture_after else "not found")

    def _texture_versions(self, name: str) -> tuple[Any, Any]:
        """(the texture as the map has it, the planned replacement or None)."""
        from valve_qc_merger.mapkit.geometry import geometry
        from valve_qc_merger.mapkit.textures import lump_entries
        from valve_qc_merger.sprite.wad import MIPTEX, read_miptex
        state = self.current()
        if state is None:
            return None, None
        after = next((op.data["texture"] for op in reversed(state.plan.active)
                      if op.code in ("textures.replace", "textures.wad_copy")
                      and str(op.data["name"]).lower() == name.lower()), None)
        tex = next((t for t in geometry(state.bsp).textures
                    if t.offset >= 0 and t.name.lower() == name.lower()), None)
        if tex is None:
            return None, after
        raw = lump_entries(state.bsp)[tex.index] if tex.embedded else next(
            (lump for w in map_wads(state.path, state.bsp, self.state.folders)
             if (lump := w.lump(tex.name)) is not None), None)
        try:
            before = read_miptex(raw, tex.name, MIPTEX) if raw else None
        except (ValueError, IndexError):
            before = None
        return before, after

    def _replace_texture(self) -> None:
        item = self.texture_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        path, _ = QFileDialog.getOpenFileName(self, f"Replace {name}", "",
                                              "Pictures (*.png *.tga *.bmp)")
        if path:
            self._texture_dropped(str(item.data(Qt.ItemDataRole.UserRole)), path)

    def _texture_dropped(self, name: str, path: str) -> None:
        state = self.current()
        if state is None:
            return
        try:
            plan = plan_replace_textures(state.path, [(name, Path(path))], self.state.folders)
        except (KeyError, ValueError, OSError) as exc:
            self._notes([str(exc.args[0] if exc.args else exc)])
            return
        state.add(plan.operations)
        self._refresh_all()

    def _embed(self, missing_only: bool) -> None:
        state = self.current()
        if state is not None:
            plan = plan_embed(state.path, missing_only=missing_only, folders=self.state.folders)
            state.add(plan.operations)
            self._notes(plan.notes)
            self._refresh_all()

    def _extract(self) -> None:
        state = self.current()
        if state is None:
            return
        name, ok = QInputDialog.getText(self, "Extract to a WAD", "WAD name (in the game folder)",
                                        text=f"{state.name}_tex.wad")
        if ok and name.strip():
            state.add(plan_extract(state.path, name.strip()).operations)
            self._refresh_all()

    def _prune(self) -> None:
        state = self.current()
        if state is None or state.report is None:
            return
        plan = plan_fixes(state.report, FixOptions(spawns=0, cso_entities="keep",
                                                   remove_missing=False), self.state.rules)
        state.add([op for op in plan.operations if op.code == "worldspawn.wad"])
        self._refresh_all()

    # -- props -----------------------------------------------------------------------

    def _props_tab(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.addWidget(kit.hint(
            "The models cycler_sprite / env_sprite entities show, merged into one model with "
            "merge-props (body picks the model): the map takes one or two model slots. The "
            "merged model is written to models/<map>/ now; the entities follow in the plan."))
        self.props_table = QTableWidget(0, 4)
        self.props_table.setHorizontalHeaderLabels(["Model", "Entities", "Group", "Status"])
        self.props_table.verticalHeader().setVisible(False)
        self.props_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)
        self.props_table.setToolTip("Type a group name (vehicles, trees …): a group's models "
                                    "go into one merged part side by side")
        self.props_table.itemChanged.connect(self._group_edited)
        column.addWidget(self.props_table, 1)
        row = QHBoxLayout()
        merge = QPushButton(icon("boxes"), "Merge props")
        theme.set_primary(merge)
        merge.clicked.connect(self._merge_props)
        self.props_summary = kit.hint("")
        row.addWidget(merge)
        row.addWidget(self.props_summary, 1)
        column.addLayout(row)
        return page

    def _groups_file(self, state: MapState) -> Path:
        return state.path.with_name(f"{state.name}_props_groups.json")

    def _groups(self, state: MapState) -> dict[str, list[str]]:
        from valve_qc_merger.mapkit.props import read_groups
        path = self._groups_file(state)
        try:
            return read_groups(path) if path.is_file() else {}
        except (OSError, ValueError):
            return {}

    def _show_props(self, state: MapState) -> None:
        from valve_qc_merger.mapkit.props import collect
        props = collect([state.path], self.state.folders)
        member = {m.lower(): g for g, ms in self._groups(state).items() for m in ms}
        self.props_table.blockSignals(True)
        self.props_table.setRowCount(len(props))
        for row, prop in enumerate(props):
            self.props_table.setItem(row, 0, _cell(prop.path))
            self.props_table.setItem(row, 1, _cell(len(prop.uses)))
            self.props_table.setItem(row, 2, QTableWidgetItem(member.get(prop.path.lower(), "")))
            status = self._prop_status.get(prop.path.lower(), "")
            self.props_table.setItem(row, 3, _cell(prop.reason or status or "merges",
                                                   "warning" if prop.reason else "success"))
        self.props_table.blockSignals(False)
        self.props_summary.setText(f"{sum(not p.reason for p in props)} of {len(props)} "
                                   "models can merge (look-alikes become skins of one)")

    def _group_edited(self, item: QTableWidgetItem) -> None:
        state = self.current()
        if state is None or item.column() != 2:
            return
        groups: dict[str, list[str]] = {}
        for row in range(self.props_table.rowCount()):
            cell = self.props_table.item(row, 2)
            name = cell.text().strip() if cell is not None else ""
            if name:
                groups.setdefault(name, []).append(self.props_table.item(row, 0).text())
        path = self._groups_file(state)
        if groups:
            path.write_text(json.dumps(groups, indent=1), encoding="utf-8")
        elif path.is_file():
            path.unlink()

    def _merge_props(self) -> None:
        from valve_qc_merger.mapkit.props import find_studiomdl, merge_props
        state = self.current()
        if state is None:
            return
        studiomdl = find_studiomdl()
        if studiomdl is None:
            self._notes(["studiomdl not found: set VQM_STUDIOMDL or the project's studiomdl"])
            return
        out = self.state.out_dir or game_folder(state.path)
        folders = list(self.state.folders)
        groups = self._groups(state) or None

        def work(reporter):  # noqa: ANN001, ANN202
            return merge_props([state.path], out, f"{state.name}_props", folders=folders,
                               studiomdl=studiomdl, reporter=reporter, groups=groups)

        self._pending["props"] = state
        self.jobs.start("Merge props", work)

    # -- build -----------------------------------------------------------------------

    def _build_tab(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        split = QSplitter(Qt.Orientation.Vertical)
        self.op_list = QListWidget()
        self.op_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        mono = self.op_list.font()
        mono.setFamilies(["Menlo", "Consolas", "DejaVu Sans Mono", "monospace"])
        self.op_list.setFont(mono)
        self.op_list.itemChanged.connect(self._op_toggled)
        split.addWidget(self.op_list)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        split.addWidget(self.log_view)
        column.addWidget(split, 1)
        row = QHBoxLayout()
        self.ent_check = QCheckBox(".ent instead of the BSP (ReHLDS sv_use_entity_file 1)")
        self.ent_check.toggled.connect(lambda on: setattr(self.state, "ent_file", on))
        out = QPushButton(icon("folder-open"), "Write into another game folder…")
        out.clicked.connect(self._choose_out)
        self.out_label = kit.hint("")
        remove = QPushButton(icon("trash-2"), "Remove selected")
        remove.clicked.connect(self._remove_ops)
        row.addWidget(self.ent_check)
        row.addWidget(out)
        row.addWidget(self.out_label, 1)
        row.addWidget(remove)
        column.addLayout(row)
        tools = QHBoxLayout()
        tools.addWidget(kit.section("Geometry (external tools)"))
        settings = X.load_settings()
        for text, tool, slot in (("Decompile…", "decompiler", self._decompile),
                                 ("Open in TrenchBroom…", "trenchbroom", self._open_editor),
                                 ("Compile .map…", "hlcsg", self._compile)):
            button = QPushButton(text)
            found = X.find_tool(tool, settings)
            button.setEnabled(found.available)
            button.setToolTip(str(found.path) if found.available else
                              f"not set up — {found.hint} (valve-qc-merger map tools --set …)")
            button.clicked.connect(slot)
            tools.addWidget(button)
        tools.addStretch(1)
        column.addLayout(tools)
        return page

    def _show_build(self, state: MapState) -> None:
        self.op_list.blockSignals(True)
        self.op_list.clear()
        for op in state.plan.operations:
            item = QListWidgetItem(f"{op.code:<16} {op.text}"
                                   + ("" if op.crc_safe else "   [CRC]")
                                   + ("   [players see it]" if op.client_visible else ""))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if op.enabled else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, id(op))
            self.op_list.addItem(item)
        self.op_list.blockSignals(False)
        self.out_label.setText(f"into {self.state.out_dir}" if self.state.out_dir
                               else "into the map's own folder (the original kept as .bak)")

    def _op_for(self, item: QListWidgetItem) -> Operation | None:
        state = self.current()
        if state is None:
            return None
        key = item.data(Qt.ItemDataRole.UserRole)
        return next((op for op in state.plan.operations if id(op) == key), None)

    def _op_toggled(self, item: QListWidgetItem) -> None:
        state, op = self.current(), self._op_for(item)
        if state is not None and op is not None:
            state.set_enabled(op, item.checkState() == Qt.CheckState.Checked)
            self._refresh_plan_bar()

    def _remove_ops(self) -> None:
        state = self.current()
        if state is None:
            return
        ops = [op for item in self.op_list.selectedItems() if (op := self._op_for(item))]
        state.remove(ops)
        self._refresh_all()

    def _choose_out(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Write maps into this game folder")
        self.state.out_dir = Path(folder) if folder else None
        self._refresh_all()

    def _decompile(self) -> None:
        state = self.current()
        if state is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Decompile to", f"{state.name}.map",
                                              "Maps (*.map)")
        if path:
            self.jobs.start("Decompile", lambda r: X.decompile_map(state.path, Path(path), r))

    def _open_editor(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open in TrenchBroom", "", "Maps (*.map)")
        if path:
            X.open_in_editor(Path(path))

    def _compile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Compile", "", "Maps (*.map)")
        if not path:
            return
        state = self.current()
        into = self.state.out_dir or (game_folder(state.path) if state else Path(path).parent)

        def work(reporter):  # noqa: ANN001, ANN202
            result = X.compile_map(Path(path), reporter=reporter)
            if result.bsp is None:
                raise RuntimeError("; ".join(result.notes + result.errors) or "no BSP")
            return X.install_map(result.bsp, into, Path(path).stem)

        self._pending["compiled"] = True
        self.jobs.start("Compile", work)

    # -- the plan bar ----------------------------------------------------------------

    def _plan_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.plan_label = QLabel("")
        self.undo_button = QPushButton(icon("step-back"), "Undo")
        self.undo_button.clicked.connect(lambda: self._history(-1))
        self.redo_button = QPushButton(icon("step-forward"), "Redo")
        self.redo_button.clicked.connect(lambda: self._history(1))
        self.apply_button = QPushButton(icon("save"), "Apply")
        theme.set_primary(self.apply_button)
        self.apply_button.clicked.connect(self.apply)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setTextVisible(True)
        self.progress.hide()
        self.cancel_button = QPushButton(icon("x"), "Cancel")
        self.cancel_button.clicked.connect(self.jobs.cancel)
        self.cancel_button.hide()
        self.jobs.started.connect(self._job_started)
        self.jobs.progress.connect(self._job_progress)
        row.addWidget(self.plan_label, 1)
        row.addWidget(self.progress)
        row.addWidget(self.cancel_button)
        row.addWidget(self.undo_button)
        row.addWidget(self.redo_button)
        row.addWidget(self.apply_button)
        return row

    def _refresh_plan_bar(self) -> None:
        state = self.current()
        if state is None:
            self.plan_label.setText("No map open")
            for button in (self.undo_button, self.redo_button, self.apply_button):
                button.setEnabled(False)
            return
        active = state.plan.active
        crc = "the CRC is kept" if state.plan.crc_safe else "the CRC changes"
        name = state.output_name(self.state.out_dir)
        rename = f" · writes {name}" if name != state.name else f" · writes {name} (same name)"
        token = "success" if not state.plan.needs_new_name else "warning"
        self.plan_label.setText(f"<span style='color:{theme.color(token).name()}'>Plan: "
                                f"{len(active)} changes · {crc}{rename}</span>")
        self.undo_button.setEnabled(state.can_undo)
        self.redo_button.setEnabled(state.can_redo)
        self.apply_button.setEnabled(bool(active))
        item = self._item_for(state)
        if item is not None:
            self._label_item(item, state)
        report = state.report
        if report is not None:
            slots = report.slots
            self.status.setText(f"Model slots {slots.get('total')} (+{slots.get('stock_models')} "
                                f"the game) · sounds {report.slots.get('sounds')} · spawns CT "
                                f"{report.spawns.get('ct')} / T {report.spawns.get('t')} · "
                                f"{state.path}")

    def _history(self, step: int) -> None:
        state = self.current()
        if state is None:
            return
        state.undo() if step < 0 else state.redo()
        self._refresh_all()

    def _add_ops(self, operations: list) -> None:
        state = self.current()
        if state is not None:
            state.add(operations)
            self._refresh_all()

    def apply(self) -> None:
        state = self.current()
        if state is None or not state.plan.active:
            return
        result = self.state.apply(state)
        self._log(f"{state.name}: wrote " + ", ".join(p.name for p in result.written)
                  + f" — CRC {result.crc_before} -> {result.crc_after}")
        self._notes(result.notes)
        item = self._item_for(state)
        if item is not None:
            self.map_list.takeItem(self.map_list.row(item))
        written = (self.state.out_dir or game_folder(state.path)) / "maps" / \
            f"{result.map_name}.bsp"
        self.open_map(written)
        self._refresh_all()

    # -- batch build, import -----------------------------------------------------------

    def _build_selected(self) -> None:
        from valve_qc_merger.mapkit.workflow import BuildOptions, build_maps
        states = self.selected()
        if not states:
            return
        paths = [str(s.path) for s in states]
        options = BuildOptions(out=self.state.out_dir, folders=list(self.state.folders))
        rules = self.state.rules
        self._pending["built"] = paths
        self.jobs.start("Build maps", lambda r: build_maps(paths, rules, options, r))

    def _import(self) -> None:
        from valve_qc_merger.mapkit.nexon import SourceIndex, import_map, write_record
        dialog = ImportDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        source, out = Path(dialog.source.text()), Path(dialog.out.text())
        names = dialog.names.text().split()
        folders = list(self.state.folders)
        if not source.is_dir() or not dialog.out.text():
            self._notes(["choose the source folder and the game folder"])
            return

        def work(reporter):  # noqa: ANN001, ANN202
            index = SourceIndex.build([source])
            wanted = names or sorted({k[5:-4] for k in index.files
                                      if k.startswith("maps/") and k.endswith(".bsp")})
            from valve_qc_merger.mapkit.nexon import ImportResult
            result = ImportResult()
            for done, name in enumerate(wanted):
                reporter.check()
                reporter.progress(done, len(wanted), name)
                reporter.log(f"importing {name}")
                import_map(name, index, out, base=folders, result=result)
            write_record(result, out)
            return [out / "maps" / f"{m}.bsp" for m in result.maps], result.notes

        self._pending["imported"] = True
        self.jobs.start("Import maps", work)

    # -- jobs ------------------------------------------------------------------------

    def _log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def _notes(self, notes: list[str]) -> None:
        for note in notes:
            self._log(f"note: {note}")
        if notes:
            self.status.setText(notes[-1])

    def _job_started(self, title: str) -> None:
        self.progress.setRange(0, 0)
        self.progress.setFormat(title)
        self.progress.show()
        self.cancel_button.show()

    def _job_progress(self, done: int, total: int, label: str) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(f"{label} %v / %m" if label else "%v / %m")

    def _job_done(self, title: str, ok: bool, payload: object) -> None:
        self.progress.hide()
        self.cancel_button.hide()
        if not ok:
            self._log(f"{title}: {payload}")
            self.status.setText(f"{title} failed: {payload}")
            self._pending.clear()
            return
        state = self._pending.pop("spawns", None)
        if isinstance(state, MapState):
            ops = [Operation("spawn.generate", f"add a {'CT' if p.classname == CT else 'T'} "
                             f"spawn at {p.origin[0]:g} {p.origin[1]:g} {p.origin[2]:g}",
                             None, {"new": p.entity()})
                   for p in payload.added]  # type: ignore[attr-defined]
            state.add(ops)
            self._notes(list(payload.notes))  # type: ignore[attr-defined]
        state = self._pending.pop("props", None)
        if isinstance(state, MapState):
            if payload.plans:  # type: ignore[attr-defined]
                state.add(payload.plans[0].operations)  # type: ignore[attr-defined]
            self.state.check(state)
            for prop in payload.models:  # type: ignore[attr-defined]
                self._prop_status[prop.path.lower()] = (
                    f"skin {prop.skin} of {prop.variant_of}" if prop.variant_of and prop.skin
                    else f"the same as {prop.variant_of}" if prop.variant_of
                    else prop.reason or "merged")
            self._log(f"props: {len(payload.merged)} models -> "  # type: ignore[attr-defined]
                      f"{', '.join(payload.parts) or 'nothing'}")  # type: ignore[attr-defined]
        if self._pending.pop("imported", None):
            paths, notes = payload  # type: ignore[misc]
            for path in paths:
                if path.is_file():
                    self.open_map(path, select=False)
            self._notes(list(notes)[:20])
        built = self._pending.pop("built", None)
        if built:
            for build in payload:  # type: ignore[attr-defined]
                level = build.after.level if build.after else ""
                self._log(f"{build.name}: {build.error or level}")
                for step in build.steps:
                    self._log(f"  {step}")
            for path in built:
                state = self.state.maps.get(Path(path))
                if state is not None:
                    self.state.check(state)
        if self._pending.pop("compiled", None):
            self.open_map(Path(str(payload)))
        self._refresh_all()

    # -- refresh ---------------------------------------------------------------------

    def _refresh_all(self) -> None:
        state = self.current()
        if state is not None:
            self._show_overview(state)
            self._show_build(state)
        self._show_tab()
        self._refresh_plan_bar()

    def _show_tab(self) -> None:
        state = self.current()
        if state is None or state.report is None:
            return
        index = self.tabs.currentIndex()
        if index == 1:
            self._show_plan(state)
        elif index == 3:
            self._show_entities(state)
        elif index == 4:
            self._show_textures(state)
        elif index == 5:
            self._show_props(state)

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        self.jobs.cancel()
        self.jobs.wait(5000)
        self.viewport.release_gl()
        super().closeEvent(event)


class TextureList(QListWidget):
    """Texture thumbnails; a picture dropped on one replaces it."""

    dropped = Signal(str, str)  # texture name, picture path

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setIconSize(QPixmap(96, 96).size())
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setGridSize(QPixmap(124, 140).size())
        self.setAcceptDrops(True)
        self.setDragEnabled(False)

    def dragEnterEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        item = self.itemAt(event.position().toPoint())
        urls = event.mimeData().urls()
        if item is not None and urls:
            self.dropped.emit(str(item.data(Qt.ItemDataRole.UserRole)), urls[0].toLocalFile())
            event.acceptProposedAction()


__all__ = ["MapWorkshop", "PlanView", "TextureList"]
