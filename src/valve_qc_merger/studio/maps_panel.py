"""Server ▸ Maps: what each map needs on the client, its .res, the mapcycle."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.project import Project
from valve_qc_merger.server import maps
from valve_qc_merger.server.bsp import BspError
from valve_qc_merger.server.doctor import fallback_folders
from valve_qc_merger.server.scan import scan_folder
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import icon

RES_TEXT = {"none": "none", "ok": "up to date", "outdated": "outdated"}
COLUMNS = ["Map", "Cycle", "Model slots", "Entities", "Needs", "Missing", ".res"]


def _cell(text: object, tip: str = "", token: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(str(text))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if tip:
        item.setToolTip(tip)
    if token:
        item.setForeground(theme.color(token))
    return item


class MapsPanel(QWidget):
    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.checks: dict[str, maps.MapCheck | None] = {}  # None: in the cycle, no BSP
        self.errors: dict[str, str] = {}
        self.cycle: list[str] = []
        self._scanned = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 12, 4, 4)
        layout.setSpacing(8)
        layout.addWidget(kit.hint(
            "Clients download what the server precaches, but not a map's texture WADs, sky, "
            "detail textures or overview: those belong in maps/<map>.res. Here every map of "
            "the game folder (and of mapcycle.txt) with what it needs, what no folder has, "
            "and whether its .res is complete."))
        row = QHBoxLayout()
        self.cycle_box = QCheckBox("Only maps of mapcycle.txt")
        self.cycle_box.toggled.connect(lambda _on: self._fill())
        self.summary = QLabel()
        rescan = QPushButton(icon("refresh-cw"), "Rescan")
        rescan.clicked.connect(self.scan)
        rescan.setAutoDefault(False)
        row.addWidget(self.summary, 1)
        row.addWidget(self.cycle_box)
        row.addWidget(rescan)
        layout.addLayout(row)
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.currentCellChanged.connect(lambda *_a: self._show_details())
        split.addWidget(self.table)
        self.details = QTableWidget(0, 3)
        self.details.setHorizontalHeaderLabels(["", "File", "Status"])
        self.details.verticalHeader().setVisible(False)
        details_header = self.details.horizontalHeader()
        details_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        details_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        details_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        split.addWidget(self.details)
        split.setSizes([320, 200])
        layout.addWidget(split, 1)
        bottom = QHBoxLayout()
        self.status = QLabel()
        theme.set_role(self.status, "muted")
        copy = QPushButton(icon("copy"), "Copy report")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.report()))
        self.write_button = QPushButton(icon("file-down", theme.TOKENS["on_accent"]),
                                        "Write .res for selected")
        theme.set_primary(self.write_button)
        self.write_button.clicked.connect(lambda: self.write_selected())
        for button in (copy, self.write_button):
            button.setAutoDefault(False)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(copy)
        bottom.addWidget(self.write_button)
        layout.addLayout(bottom)

    def showEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        super().showEvent(event)
        if not self._scanned:
            self.scan()

    def game_dir(self) -> Path | None:
        game = self.project.settings.game_dir
        return Path(game) if game and Path(game).is_dir() else None

    def scan(self) -> None:
        self._scanned = True
        self.checks, self.errors, self.cycle = {}, {}, []
        game = self.game_dir()
        if game is None:
            self.summary.setText("Set the game folder in Project ▸ Settings.")
            self._fill()
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            inventory = scan_folder(game)
            fallback = [scan_folder(f) for f in fallback_folders(game)]
            self.cycle = maps.read_mapcycle(game / "mapcycle.txt")
            for path in sorted((game / "maps").glob("*.bsp"), key=lambda p: p.name.lower()):
                try:
                    self.checks[path.stem] = maps.check_map(path, inventory, fallback)
                except (BspError, OSError) as exc:
                    self.errors[path.stem] = str(exc)
            for name in self.cycle:
                if name not in self.checks and name not in self.errors:
                    self.checks[name] = None
        finally:
            QApplication.restoreOverrideCursor()
        broken = sum(1 for c in self.checks.values() if c is not None and c.missing)
        lost = [n for n, c in self.checks.items() if c is None]
        stale = sum(1 for c in self.checks.values()
                    if c is not None and c.to_write and c.res_state != "ok")
        parts = [f"{len(self.checks) - len(lost)} map(s)"]
        if self.cycle:
            parts.append(f"{len(self.cycle)} in mapcycle")
        if lost:
            parts.append(f"{len(lost)} in the cycle without a BSP")
        if broken:
            parts.append(f"{broken} missing files")
        if stale:
            parts.append(f"{stale} need a .res")
        self.summary.setText("  ·  ".join(parts))
        self._fill()

    def shown(self) -> list[str]:
        names = list(self.checks) + list(self.errors)
        if self.cycle_box.isChecked():
            cycle = {n.lower() for n in self.cycle}
            names = [n for n in names if n.lower() in cycle]
        return sorted(names, key=str.lower)

    def _fill(self) -> None:
        names = self.shown()
        cycle = {n.lower() for n in self.cycle}
        self.table.setRowCount(len(names))
        for row, name in enumerate(names):
            check = self.checks.get(name)
            in_cycle = "✓" if name.lower() in cycle else ""
            self.table.setItem(row, 0, _cell(name))
            self.table.setItem(row, 1, _cell(in_cycle))
            if name in self.errors:
                for column in range(2, len(COLUMNS)):
                    self.table.setItem(row, column, _cell(""))
                self.table.setItem(row, 6, _cell("unreadable", self.errors[name], "danger"))
                continue
            if check is None:
                for column in range(2, len(COLUMNS)):
                    self.table.setItem(row, column, _cell(""))
                self.table.setItem(row, 6, _cell("no BSP", "mapcycle.txt names it, but "
                                                 "maps/ has no such map", "danger"))
                continue
            needs = check.needs
            self.table.setItem(row, 2, _cell(needs.model_slots))
            self.table.setItem(row, 3, _cell(needs.entities))
            self.table.setItem(row, 4, _cell(len(needs.res)))
            self.table.setItem(row, 5, _cell(len(check.missing) or "",
                                             "\n".join(check.missing),
                                             "warning" if check.missing else None))
            if not check.to_write:
                state, token = "not needed", "muted"
            else:
                state = RES_TEXT[check.res_state]
                token = "success" if check.res_state == "ok" else "warning"
            self.table.setItem(row, 6, _cell(state, "", token))
        self._show_details()

    def current(self) -> str | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.text() if item is not None else None

    def _show_details(self) -> None:
        name = self.current()
        check = self.checks.get(name) if name else None
        rows: list[tuple[str, str, str, str]] = []
        if check is not None:
            missing = {p.lower() for p in check.missing}
            stock = {p.lower() for p in check.stock}
            listed = {p.lower() for p in check.res_listed}
            for path in check.needs.res + check.needs.precached:
                low = path.lower()
                kind = "res" if path in check.needs.res else "precached"
                if low in missing:
                    rows.append(("✕", path, f"missing ({kind})", "danger"))
                elif low in stock:
                    rows.append(("●", path, "base game (every client has it)", "muted"))
                elif kind == "precached":
                    rows.append(("✓", path, "precached (sent anyway)", "success"))
                elif low in listed:
                    rows.append(("✓", path, "in .res", "success"))
                else:
                    rows.append(("▲", path, "not in .res yet", "warning"))
            rows += [("▲", path, "in .res, but no folder has it", "warning")
                     for path in check.res_extra]
        self.details.setRowCount(len(rows))
        for row, (glyph, path, status, token) in enumerate(rows):
            self.details.setItem(row, 0, _cell(glyph, "", token))
            self.details.setItem(row, 1, _cell(path, path))
            self.details.setItem(row, 2, _cell(status))

    def selected(self) -> list[str]:
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        return [self.table.item(r, 0).text() for r in sorted(rows)]

    def write_selected(self, names: list[str] | None = None) -> list[Path]:
        game = self.game_dir()
        if game is None:
            return []
        written = []
        for name in names if names is not None else self.selected():
            check = self.checks.get(name)
            if check is None:
                continue
            path = maps.write_res(check, game / "maps" / f"{name}.bsp")
            if path is not None:
                written.append(path)
        current = self.current()
        self.scan()
        if current is not None:
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0).text() == current:
                    self.table.selectRow(row)
        self.status.setText(f"Wrote {len(written)} .res file(s)" if written
                            else "Nothing to write: the selected maps need no extra files.")
        return written

    def report(self) -> str:
        lines = []
        for name in self.shown():
            check = self.checks.get(name)
            if name in self.errors:
                lines.append(f"{name}: unreadable ({self.errors[name]})")
            elif check is None:
                lines.append(f"{name}: in mapcycle.txt, no BSP")
            else:
                lines.append(f"{name}: {check.needs.model_slots} model slots, .res "
                             f"{RES_TEXT[check.res_state] if check.to_write else 'not needed'}")
                lines += [f"  missing {p}" for p in check.missing]
        return "\n".join(lines) + "\n"


__all__ = ["MapsPanel"]
