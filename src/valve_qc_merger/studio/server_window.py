"""Project ▸ Server: precache budget per map, mod-folder doctor, unprecache, cfgs."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.project import Project
from valve_qc_merger.server.bsp import BspError, MapResources, read_map_resources
from valve_qc_merger.server.budget import BudgetLine, budget, map_files, project_load
from valve_qc_merger.server.doctor import SEVERITIES, Issue, check_folder, summary
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import icon

NO_MAP = "No map — the project and your estimates"
SEVERITY_GLYPH = {"error": "✕", "warning": "▲", "info": "●"}
SEVERITY_TOKEN = {"error": "danger", "warning": "warning", "info": "muted"}
LEVEL_TOKEN = {"ok": "success", "warning": "warning", "error": "danger"}


def _item(text: object, tip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(str(text))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if tip:
        item.setToolTip(tip)
    return item


class BudgetBar(QWidget):
    """One limit: title, used / limit, a bar coloured by level, the parts."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(2)
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: 600;")
        self.numbers = QLabel()
        self.numbers.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.parts = kit.hint("")
        # a wrapped label in a scroll area gets squeezed to one line unless it
        # asks for the height its width needs
        self.parts.setSizePolicy(QSizePolicy.Policy.Preferred,
                                 QSizePolicy.Policy.MinimumExpanding)
        layout.addWidget(self.title, 0, 0)
        layout.addWidget(self.numbers, 0, 1)
        layout.addWidget(self.bar, 1, 0, 1, 2)
        layout.addWidget(self.parts, 2, 0, 1, 2)
        layout.setColumnStretch(0, 1)

    def show_line(self, line: BudgetLine) -> None:
        self.title.setText(line.title)
        used = line.used
        free = line.limit - used
        self.numbers.setText(f"{used} / {line.limit}" + (f"   ({free} free)" if free >= 0
                                                         else f"   ({-free} over!)"))
        self.numbers.setStyleSheet(f"color: {theme.TOKENS[LEVEL_TOKEN[line.level]]};")
        self.bar.setRange(0, line.limit)
        self.bar.setValue(min(used, line.limit))
        self.bar.setProperty("level", line.level)
        self.bar.style().unpolish(self.bar)
        self.bar.style().polish(self.bar)
        self.parts.setText("  ·  ".join(f"{label} {count}" for label, count in line.parts
                                        if count))


class ServerWindow(QDialog):
    """Budget (slots per map) and Doctor (problems in the mod folder)."""

    settings_changed = Signal()

    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.setWindowTitle("Server")
        self._maps: dict[str, MapResources | None] = {}
        self.issues: list[Issue] = []
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header(
            "Server", "Precache budgets, mod-folder health, configs, map entities and the "
            "download mirror for ReHLDS + ReGameDLL + ReAPI: models 512, sounds 512, "
            "generic 4096."))
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.tabs.addTab(self._budget_tab(), icon("gauge"), "Budget")
        self.tabs.addTab(self._doctor_tab(), icon("stethoscope"), "Doctor")
        self.tabs.addTab(self._unprecache_tab(), icon("trash-2"), "Unprecache")
        from valve_qc_merger.studio.config_panel import ConfigPanel
        self.config = ConfigPanel(project)
        self.tabs.addTab(self.config, icon("sliders-horizontal"), "Config")
        from valve_qc_merger.studio.entities_panel import EntitiesPanel
        self.entities = EntitiesPanel(project)
        self.tabs.addTab(self.entities, icon("map"), "Entities")
        from valve_qc_merger.studio.maps_panel import MapsPanel
        self.maps_panel = MapsPanel(project)
        self.tabs.addTab(self.maps_panel, icon("layers"), "Maps")
        from valve_qc_merger.studio.download_panel import DownloadPanel
        self.download = DownloadPanel(project)
        self.tabs.addTab(self.download, icon("file-down"), "Download")
        from valve_qc_merger.studio.fastdl_panel import FastDlPanel
        self.fastdl = FastDlPanel(project)
        self.tabs.addTab(self.fastdl, icon("rocket"), "FastDL")
        from valve_qc_merger.studio.logs_panel import LogsPanel
        self.logs = LogsPanel(project, self.show_tab)
        self.tabs.addTab(self.logs, icon("square-terminal"), "Logs")
        self.resize(1040, 820)
        self.refresh()

    def show_tab(self, key: str) -> None:
        """Bring the tab for ``key`` (budget, doctor, unprecache, config,
        entities, maps, download, fastdl, logs) to the front."""
        pages = {"budget": 0, "doctor": 1, "unprecache": 2, "config": self.config,
                 "entities": self.entities, "maps": self.maps_panel,
                 "download": self.download, "fastdl": self.fastdl, "logs": self.logs}
        page = pages.get(key)
        if isinstance(page, int):
            self.tabs.setCurrentIndex(page)
        elif page is not None:
            self.tabs.setCurrentWidget(page)

    # -- budget --------------------------------------------------------------
    def _budget_tab(self) -> QWidget:
        from PySide6.QtWidgets import QFrame, QScrollArea
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        scroll.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 12, 4, 4)
        layout.setSpacing(8)
        row = kit.form()
        self.map_box = QComboBox()
        self.map_box.currentIndexChanged.connect(lambda _i: self._show_budget())
        open_map = QPushButton(icon("folder-open"), "Open BSP…")
        open_map.setAutoDefault(False)
        open_map.setToolTip("Count a map that is not in the game folder's maps/")
        open_map.clicked.connect(self._open_map)
        map_row = QHBoxLayout()
        map_row.addWidget(self.map_box, 1)
        map_row.addWidget(open_map)
        row.addRow("Map", map_row)
        layout.addLayout(row)
        self.verdict = QLabel()
        self.verdict.setTextFormat(Qt.TextFormat.RichText)
        self.verdict.setStyleSheet("font-weight: 600;")
        self.verdict.setWordWrap(True)
        layout.addWidget(self.verdict)
        self.no_game_hint = kit.hint("Set the game folder in Project ▸ Settings to count "
                                     "each map's brush models, sprites and sounds.")
        layout.addWidget(self.no_game_hint)

        layout.addWidget(kit.section("Game DLL + plugins"))
        from PySide6.QtWidgets import QCheckBox

        from valve_qc_merger.server.stock import STOCK_MODELS, STOCK_SOUNDS
        self.stock_box = QCheckBox(
            f"Count what ReGameDLL precaches on every map: {STOCK_MODELS} models and "
            f"sprites, {STOCK_SOUNDS} sounds (minus what you unprecache)")
        self.stock_box.setToolTip(
            "Read from ReGameDLL's source: weapons (v_/p_/w_, shields), player models, "
            "items, gibs, shells, effect sprites; weapon, player, radio and impact "
            "sounds. Not counted: bots (only with bots allowed) and what a map's "
            "entities add when they spawn.")
        self.stock_box.setChecked(self.project.settings.count_stock)
        self.stock_box.toggled.connect(lambda _on: self._estimates_changed())
        layout.addWidget(self.stock_box)
        layout.addWidget(kit.hint(
            "The studio sees the map, this project and ReGameDLL — not what your AMXX "
            "plugins precache: enter those counts (rescount / reslist on a ReHLDS server "
            "with every plugin loaded, minus the map and ReGameDLL) for a true total."))
        estimates = QHBoxLayout()
        self.extra: dict[str, QSpinBox] = {}
        for key, title in (("models", "Models"), ("sounds", "Sounds"),
                           ("generic", "Generic")):
            spin = QSpinBox()
            spin.setRange(0, 9999)
            spin.setValue(int(getattr(self.project.settings, f"extra_{key}")))
            spin.valueChanged.connect(self._estimates_changed)
            self.extra[key] = spin
            estimates.addWidget(QLabel(title))
            estimates.addWidget(spin)
            estimates.addSpacing(10)
        self.client_box = QComboBox()
        self.client_box.addItem("Client sounds → precache_generic (ReHLDS)", "generic")
        self.client_box.addItem("Client sounds → precache_sound", "sound")
        self.client_box.setCurrentIndex(0 if self.project.settings.client_sounds != "sound"
                                        else 1)
        self.client_box.setToolTip(
            "Where your weapon plugin precaches the sounds view models play (event "
            "5004). On ReHLDS generic has 4096 slots, so they do not eat the 512 sounds.")
        self.client_box.currentIndexChanged.connect(self._estimates_changed)
        estimates.addStretch(1)
        layout.addLayout(estimates)
        layout.addWidget(self.client_box, 0, Qt.AlignmentFlag.AlignLeft)

        layout.addWidget(kit.section("Slots"))
        self.bars = {key: BudgetBar() for key in ("models", "sounds", "generic")}
        for bar in self.bars.values():
            layout.addWidget(bar)
        self.saved_label = QLabel()
        theme.set_role(self.saved_label, "success")
        self.saved_label.setWordWrap(True)
        layout.addWidget(self.saved_label)

        self.budget_tabs = QTabWidget()
        self.merge_table = QTableWidget(0, 5)
        self.merge_table.setHorizontalHeaderLabels(["Models", "As imported", "After merge",
                                                    "Saves", "State"])
        self.merge_table.verticalHeader().setVisible(False)
        merge_header = self.merge_table.horizontalHeader()
        merge_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        merge_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.merge_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.merge_table.setToolTip(
            "As imported: every weapon a model slot of its own (a model and its swap-hands "
            "copy are one weapon). After merge: each build's parts — from its last run, "
            "else its plan — and the models no build takes, as they are.")
        self.budget_tabs.addTab(self.merge_table, "As imported → after merge")
        self.maps_table = QTableWidget(0, 6)
        self.maps_table.setHorizontalHeaderLabels(["Map", "Models", "As imported", "Sounds",
                                                   "Generic", "Status"])
        self.maps_table.verticalHeader().setVisible(False)
        self.maps_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.maps_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.maps_table.cellDoubleClicked.connect(
            lambda row, _c: self.map_box.setCurrentText(self.maps_table.item(row, 0).text()))
        self.budget_tabs.addTab(self.maps_table, "Every map")
        self.budget_tabs.setMinimumHeight(260)
        layout.addWidget(self.budget_tabs, 1)
        return scroll

    def _estimates_changed(self) -> None:
        settings = self.project.settings
        for key, spin in self.extra.items():
            setattr(settings, f"extra_{key}", spin.value())
        settings.client_sounds = self.client_box.currentData()
        settings.count_stock = self.stock_box.isChecked()
        self.project.save()
        self.settings_changed.emit()
        self._show_budget()
        self._fill_maps_table()

    def refresh(self) -> None:
        """Re-read the maps and the builds (after a build ran, settings changed)."""
        from valve_qc_merger.server.budget import merge_comparison
        self.load = project_load(self.project)
        self.comparison = merge_comparison(self.project, self.load)
        game = Path(self.project.settings.game_dir) if self.project.settings.game_dir else None
        self._maps = {}
        for path in map_files(game):
            try:
                self._maps[path.stem] = read_map_resources(path)
            except (BspError, OSError):
                self._maps[path.stem] = None
        current = self.map_box.currentText()
        self.map_box.blockSignals(True)
        self.map_box.clear()
        self.map_box.addItem(NO_MAP)
        for name in self._maps:
            self.map_box.addItem(name)
        if current and self.map_box.findText(current) >= 0:
            self.map_box.setCurrentText(current)
        self.map_box.blockSignals(False)
        self.no_game_hint.setVisible(not self._maps)
        self.doctor_folder.setText(self.project.settings.game_dir or "")
        self._show_budget()
        self._fill_maps_table()

    def _lines(self, map_resources: MapResources | None,
               as_imported: bool = False) -> dict[str, BudgetLine]:
        extra = {key: spin.value() for key, spin in self.extra.items()}
        from valve_qc_merger.server.stock import STOCK_MODELS, STOCK_SOUNDS
        return budget(map_resources, self.load, extra=extra,
                      client_sounds_as=self.client_box.currentData(),
                      unprecached=self.freed_slots() if self.stock_box.isChecked() else 0,
                      stock_models=STOCK_MODELS if self.stock_box.isChecked() else 0,
                      stock_sounds=STOCK_SOUNDS if self.stock_box.isChecked() else 0,
                      unmerged=self.comparison.unmerged,
                      left_out=self.comparison.left_out,
                      as_imported=self.comparison.imported if as_imported else None,
                      client_sounds=(self.comparison.sounds_imported if as_imported
                                     else self.comparison.sounds_merged))

    def _open_map(self) -> None:
        start = self.project.settings.game_dir or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Count a map", start, "Maps (*.bsp)")
        if path:
            self.add_map(Path(path))

    def add_map(self, path: Path) -> None:
        """Count ``path`` too (a map outside the game folder) and show it."""
        try:
            self._maps[path.stem] = read_map_resources(path)
        except (BspError, OSError):
            self._maps[path.stem] = None
        if self.map_box.findText(path.stem) < 0:
            self.map_box.addItem(path.stem)
        self.map_box.setCurrentText(path.stem)
        self._fill_maps_table()

    def _show_budget(self) -> None:
        name = self.map_box.currentText()
        lines = self._lines(self._maps.get(name) if name != NO_MAP else None)
        for key, bar in self.bars.items():
            bar.show_line(lines[key])
        where = f"With {name}" if name != NO_MAP else "Without a map"
        if name != NO_MAP and self._maps.get(name) is None:
            self.verdict.setText(self._coloured(f"{name} cannot be read.", "danger"))
        else:
            before = self._lines(self._maps.get(name) if name != NO_MAP else None, True)
            after_text, after_level = self._verdict(lines)
            text = self._coloured(f"{where}, after merge: {after_text}", after_level)
            comparison = self.comparison
            if comparison.imported != comparison.merged:
                before_text, before_level = self._verdict(before)
                text += "<br>" + self._coloured(f"As imported (no merge): {before_text}",
                                                before_level)
            if name == NO_MAP:
                text += "<br>" + self._coloured(
                    "Pick a map: brush models often take 100–300.", "muted")
            self.verdict.setText(text)
        self._fill_merge_table()

    @staticmethod
    def _coloured(text: str, token: str) -> str:
        from html import escape
        return f'<span style="color:{theme.TOKENS[token]}">{escape(text)}</span>'

    def verdict_text(self) -> str:
        """The verdict as plain text (one line per scenario)."""
        from PySide6.QtGui import QTextDocument
        document = QTextDocument()
        document.setHtml(self.verdict.text())
        return document.toPlainText()

    @staticmethod
    def _verdict(lines: dict[str, BudgetLine]) -> tuple[str, str]:
        over = [(line.title, line.used - line.limit) for line in lines.values()
                if line.used > line.limit]
        models = lines["models"]
        free = models.limit - models.used
        if over:
            return ("over the limit — " + ", ".join(f"{title.lower()} by {count}"
                                                    for title, count in over), "danger")
        return (f"fits — {free} model slot(s) free",
                "warning" if free < models.limit // 10 else "success")

    def _fill_merge_table(self) -> None:
        comparison = self.comparison
        rows = comparison.rows
        self.merge_table.setRowCount(len(rows) + 1 if rows else 0)
        state_tip = {"run": "parts of its last run", "planned": "parts of its plan",
                     "not run": "not run or planned yet: counted as imported",
                     "no build": "in no build: they stay as they are"}
        for row, entry in enumerate(rows):
            self.merge_table.setItem(row, 0, _item(entry.title))
            self.merge_table.setItem(row, 1, _item(entry.imported))
            self.merge_table.setItem(row, 2, _item(entry.merged))
            saves = _item(entry.saved or "")
            saves.setForeground(theme.color("success"))
            self.merge_table.setItem(row, 3, saves)
            text, tip = entry.state, state_tip[entry.state]
            if entry.added:
                text += f" · {len(entry.added)} added since"
                tip += (f"\n{len(entry.added)} model(s) came into the build after its last "
                        "run — run it again to merge them (counted as they are until "
                        "then):\n" + ", ".join(entry.added[:40])
                        + (" …" if len(entry.added) > 40 else ""))
            if entry.left_out:
                text += f" · {len(entry.left_out)} left out"
                tip += (f"\nThe merge left out {len(entry.left_out)} model(s) — counted as "
                        "they are (a slot each) if they stay on the server:\n"
                        + ", ".join(entry.left_out[:40])
                        + (" …" if len(entry.left_out) > 40 else ""))
            state = _item(text, tip)
            state.setForeground(theme.color("muted" if entry.state in ("run", "planned")
                                            and not entry.left_out and not entry.added
                                            else "warning"))
            self.merge_table.setItem(row, 4, state)
        if rows:
            last = len(rows)
            for column, value in enumerate(("Total", comparison.imported, comparison.merged,
                                            comparison.imported - comparison.merged or "",
                                            "")):
                cell = _item(value)
                font = cell.font()
                font.setBold(True)
                cell.setFont(font)
                self.merge_table.setItem(last, column, cell)
        saved = comparison.imported - comparison.merged
        parts = []
        if saved > 0:
            parts.append(f"Merging saves {saved} model slot(s): {comparison.imported} as "
                         f"imported → {comparison.merged}.")
        elif comparison.imported:
            parts.append(f"The project's {comparison.imported} model(s) take a slot each — "
                         "no build merges them yet.")
        left_out = sum(len(r.left_out) for r in comparison.rows)
        if left_out:
            parts.append(f"{left_out} model(s) the merges left out count as they are "
                         "(see State).")
        if comparison.pending:
            parts.append("Not run or planned yet (counted as imported): "
                         + ", ".join(comparison.pending) + " — Plan shows the parts.")
        if comparison.sounds_imported:
            parts.append(f"Sounds the models play: {comparison.sounds_imported} as imported, "
                         f"{comparison.sounds_merged} after merge (merging does not drop "
                         "sounds; client sounds go to generic on ReHLDS).")
        self.saved_label.setText("  ".join(parts))

    def _fill_maps_table(self) -> None:
        self.maps_table.setRowCount(len(self._maps))
        for row, (name, res) in enumerate(self._maps.items()):
            lines = self._lines(res)
            self.maps_table.setItem(row, 0, _item(name))
            before = self._lines(res, True)["models"]
            cell = _item(f"{before.used} / {before.limit}")
            cell.setForeground(theme.color(LEVEL_TOKEN[before.level]))
            self.maps_table.setItem(row, 2, cell)
            worst = "ok"
            for column, key in zip((1, 3, 4), ("models", "sounds", "generic"), strict=True):
                line = lines[key]
                cell = _item(f"{line.used} / {line.limit}")
                cell.setForeground(theme.color(LEVEL_TOKEN[line.level]))
                self.maps_table.setItem(row, column, cell)
                if line.level == "error" or (line.level == "warning" and worst == "ok"):
                    worst = line.level
            status = {"ok": "fits", "warning": "near a limit",
                      "error": "over a limit"}[worst] if res is not None else "unreadable"
            cell = _item(status)
            cell.setForeground(theme.color(LEVEL_TOKEN[worst] if res is not None
                                           else "muted"))
            self.maps_table.setItem(row, 5, cell)

    # -- unprecache ----------------------------------------------------------
    def _unprecache_tab(self) -> QWidget:
        from PySide6.QtWidgets import QCheckBox, QTreeWidget, QTreeWidgetItem

        from valve_qc_merger.server.stock import GROUPS, existing
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 12, 4, 4)
        layout.setSpacing(8)
        layout.addWidget(kit.hint(
            "Stock models ReGameDLL precaches that your server no longer shows still take "
            "model slots. Tick what your plugins replace; export a list.ini for Metamod "
            "Unprecacher (addons/unprecacher/list.ini). Replacing beats blocking: the game "
            "still sets those models on entities, and a blocked model with no replacement "
            "crashes clients."))
        self.unprecache_tree = QTreeWidget()
        self.unprecache_tree.setHeaderHidden(True)
        game = Path(self.project.settings.game_dir) if self.project.settings.game_dir else None
        chosen = {p.lower() for p in self.project.settings.unprecache}
        self.unprecache_tree.blockSignals(True)
        for _key, (title, why, paths) in GROUPS.items():
            files = existing(paths, game)
            if not files:
                continue
            group = QTreeWidgetItem(self.unprecache_tree, [f"{title}  ·  {len(files)}"])
            group.setToolTip(0, f"Tick when {why}.")
            group.setFlags(group.flags() | Qt.ItemFlag.ItemIsUserCheckable
                           | Qt.ItemFlag.ItemIsAutoTristate)
            for path in files:
                item = QTreeWidgetItem(group, [path])
                item.setData(0, Qt.ItemDataRole.UserRole, path)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked if path.lower() in chosen
                                   else Qt.CheckState.Unchecked)
        self.unprecache_tree.blockSignals(False)
        self.unprecache_tree.itemChanged.connect(lambda *_a: self._unprecache_changed())
        layout.addWidget(self.unprecache_tree, 1)
        row = QHBoxLayout()
        self.replace_box = QCheckBox("Replace them with")
        self.replace_edit = QLineEdit(self.project.settings.unprecache_replace)
        self.replace_edit.setPlaceholderText("e.g. models/w_supplybox.mdl")
        self.replace_box.setChecked(bool(self.project.settings.unprecache_replace))
        self.replace_box.toggled.connect(lambda _on: self._unprecache_changed())
        self.replace_edit.editingFinished.connect(self._unprecache_changed)
        row.addWidget(self.replace_box)
        row.addWidget(self.replace_edit, 1)
        layout.addLayout(row)
        bottom = QHBoxLayout()
        self.freed_label = QLabel()
        theme.set_role(self.freed_label, "success")
        export = QPushButton(icon("file-down"), "Export list.ini…")
        export.setAutoDefault(False)
        export.clicked.connect(self.export_unprecache)
        bottom.addWidget(self.freed_label, 1)
        bottom.addWidget(export)
        layout.addLayout(bottom)
        self._update_freed()
        return page

    def unprecache_entries(self) -> list:
        from valve_qc_merger.server.unprecache import Entry
        replace = self.replace_edit.text().strip() if self.replace_box.isChecked() else ""
        out = []
        root = self.unprecache_tree.invisibleRootItem()
        for g in range(root.childCount()):
            group = root.child(g)
            for i in range(group.childCount()):
                item = group.child(i)
                if item.checkState(0) == Qt.CheckState.Checked:
                    out.append(Entry(item.data(0, Qt.ItemDataRole.UserRole), replace))
        return out

    def freed_slots(self) -> int:
        from valve_qc_merger.server.unprecache import slots_freed
        if not hasattr(self, "unprecache_tree"):
            return 0
        return max(slots_freed(self.unprecache_entries()), 0)

    def _update_freed(self) -> None:
        count = len(self.unprecache_entries())
        freed = self.freed_slots()
        self.freed_label.setText(
            f"{count} stock model(s) unprecached — frees {freed} model slot(s)"
            if count else "Nothing ticked yet.")

    def _unprecache_changed(self) -> None:
        settings = self.project.settings
        entries = self.unprecache_entries()
        settings.unprecache = [e.path for e in entries]
        settings.unprecache_replace = (self.replace_edit.text().strip()
                                       if self.replace_box.isChecked() else "")
        self.project.save()
        self._update_freed()
        self._show_budget()
        self._fill_maps_table()

    def export_unprecache(self) -> str | None:
        from valve_qc_merger.server.unprecache import list_ini
        entries = self.unprecache_entries()
        if not entries:
            self.freed_label.setText("Tick at least one model to unprecache.")
            return None
        start = (Path(self.project.settings.game_dir) / "addons" / "unprecacher" / "list.ini"
                 if self.project.settings.game_dir else self.project.root / "list.ini")
        path, _ = QFileDialog.getSaveFileName(self, "Export list.ini", str(start),
                                              "Unprecacher list (*.ini)")
        if not path:
            return None
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(list_ini(entries, title=self.project.name), encoding="utf-8")
        self.freed_label.setText(f"Saved {path}")
        return path

    # -- doctor --------------------------------------------------------------
    def _doctor_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 12, 4, 4)
        layout.setSpacing(8)
        layout.addWidget(kit.hint(
            "Scans a mod folder (models, sprites, sound, maps) for what breaks or bloats a "
            "server: missing T.mdl / 01.mdl companions and sounds models play, paths over "
            "63 characters, upper case (the Linux server is case-sensitive), non-ASCII, "
            "oversized textures, sound formats the engine mangles, identical files."))
        row = QHBoxLayout()
        self.doctor_folder = QLineEdit()
        self.doctor_folder.setPlaceholderText("mod folder, e.g. …/cstrike")
        pick = QPushButton(icon("folder-open"), "")
        pick.setToolTip("Choose the folder")
        pick.clicked.connect(self._pick_folder)
        self.scan_button = QPushButton(icon("stethoscope", theme.TOKENS["on_accent"]), "Scan")
        theme.set_primary(self.scan_button)
        self.scan_button.clicked.connect(self.scan)
        for button in (pick, self.scan_button):
            button.setAutoDefault(False)  # Enter in the path field must not pick a folder
        row.addWidget(self.doctor_folder, 1)
        row.addWidget(pick)
        row.addWidget(self.scan_button)
        layout.addLayout(row)
        filters = QHBoxLayout()
        self.summary_label = QLabel("Not scanned yet.")
        self.severity_box = QComboBox()
        self.severity_box.addItem("Everything", "")
        for severity in SEVERITIES:
            self.severity_box.addItem(f"{severity.capitalize()}s only", severity)
        self.severity_box.currentIndexChanged.connect(lambda _i: self._fill_issues())
        self.issue_filter = QLineEdit()
        self.issue_filter.setPlaceholderText("Filter by path or text")
        self.issue_filter.setClearButtonEnabled(True)
        self.issue_filter.textChanged.connect(lambda _t: self._fill_issues())
        copy = QPushButton(icon("copy"), "Copy report")
        copy.setAutoDefault(False)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.report()))
        filters.addWidget(self.summary_label, 1)
        filters.addWidget(self.severity_box)
        filters.addWidget(self.issue_filter)
        filters.addWidget(copy)
        layout.addLayout(filters)
        self.issues_table = QTableWidget(0, 4)
        self.issues_table.setHorizontalHeaderLabels(["", "Kind", "File", "Problem"])
        self.issues_table.verticalHeader().setVisible(False)
        header = self.issues_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.issues_table.setColumnWidth(2, 260)
        self.issues_table.setWordWrap(False)
        self.issues_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.issues_table.setToolTip("Double-click: show the file in its folder")
        self.issues_table.cellDoubleClicked.connect(self._reveal_issue)
        layout.addWidget(self.issues_table, 1)
        return page

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Mod folder", self.doctor_folder.text())
        if folder:
            self.doctor_folder.setText(folder)

    def scan(self) -> None:
        folder = Path(self.doctor_folder.text().strip())
        if not folder.is_dir():
            self.summary_label.setText("Choose an existing mod folder first.")
            return
        from valve_qc_merger.sound.wav import wav_problems
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.issues = check_folder(folder, wav_check=wav_problems)
        finally:
            QApplication.restoreOverrideCursor()
        counts = summary(self.issues)
        self.summary_label.setText(
            "Nothing to fix." if not self.issues else
            "  ·  ".join(f"{SEVERITY_GLYPH[s]} {counts[s]} {s}{'s' * (counts[s] != 1)}"
                         for s in SEVERITIES if counts[s]))
        self._fill_issues()

    def shown_issues(self) -> list[Issue]:
        severity = self.severity_box.currentData()
        needle = self.issue_filter.text().strip().lower()
        return [i for i in self.issues
                if (not severity or i.severity == severity)
                and (not needle or needle in i.path.lower() or needle in i.message.lower())]

    def _fill_issues(self) -> None:
        shown = self.shown_issues()
        self.issues_table.setRowCount(len(shown))
        for row, issue in enumerate(shown):
            glyph = _item(SEVERITY_GLYPH[issue.severity], issue.severity)
            glyph.setForeground(theme.color(SEVERITY_TOKEN[issue.severity]))
            self.issues_table.setItem(row, 0, glyph)
            self.issues_table.setItem(row, 1, _item(issue.category))
            self.issues_table.setItem(row, 2, _item(issue.path, issue.path))
            self.issues_table.setItem(row, 3, _item(issue.message, issue.message))

    def _reveal_issue(self, row: int, _column: int) -> None:
        item = self.issues_table.item(row, 2)
        if item is None:
            return
        target = Path(self.doctor_folder.text().strip()) / item.text()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent)))

    def report(self) -> str:
        return "\n".join(f"[{i.severity}] {i.category}: {i.path} — {i.message}"
                         for i in self.shown_issues())


__all__ = ["ServerWindow"]
