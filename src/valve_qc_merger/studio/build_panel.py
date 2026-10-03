"""Build editing and results: the right-hand panel when a build is selected."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.project import BUILD_KINDS, Build, Project, ProjectError
from valve_qc_merger.services.retarget import RetargetOptions
from valve_qc_merger.studio.build_report import LIMITS, load_record, manifest_rows
from valve_qc_merger.studio.options_form import OptionsForm
from valve_qc_merger.studio.widgets import KIND_TITLES, _fill, _table

OK_COLOR = QColor("#3fa34d")
BAD_COLOR = QColor("#d64545")


def _asset_list(project: Project, kind: str, chosen: list[str]) -> QListWidget:
    accepted = BUILD_KINDS[kind].asset_kinds
    widget = QListWidget()
    for asset in sorted(project.assets.values(), key=lambda a: (a.kind, a.name)):
        if asset.kind not in accepted:
            continue
        item = QListWidgetItem(asset.name)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if asset.name in chosen
                           else Qt.CheckState.Unchecked)
        widget.addItem(item)
    return widget


def _checked(widget: QListWidget) -> list[str]:
    return [widget.item(i).text() for i in range(widget.count())
            if widget.item(i).checkState() == Qt.CheckState.Checked]


class NewBuildDialog(QDialog):
    """Name + kind; assets and options are edited in the build panel."""

    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New build")
        self.project = project
        self.name_edit = QLineEdit()
        self.kind_box = QComboBox()
        for kind, spec in BUILD_KINDS.items():
            titles = ", ".join(KIND_TITLES[k] for k in sorted(spec.asset_kinds))
            count = sum(1 for a in project.assets.values() if a.kind in spec.asset_kinds)
            self.kind_box.addItem(f"{kind}  —  {titles} ({count})", kind)
        self.kind_box.currentIndexChanged.connect(self._suggest)
        self.retarget_box = QCheckBox("put every view model on our hands first (shared hands; "
                                      "already retargeted ones are kept as they are)")
        self.category_box = QComboBox()
        self.category_box.addItem("every category", None)
        for category in sorted(project.categories, key=str.lower):
            self.category_box.addItem(category, category)
        self.category_box.currentIndexChanged.connect(self._suggest)
        form = QFormLayout(self)
        form.addRow("Kind", self.kind_box)
        form.addRow("Assets", self.category_box)
        form.addRow("Name", self.name_edit)
        form.addRow("", self.retarget_box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        best = max(range(self.kind_box.count()), key=lambda i: sum(
            1 for a in project.assets.values()
            if a.kind in BUILD_KINDS[self.kind_box.itemData(i)].asset_kinds))
        self.kind_box.setCurrentIndex(best)
        self._suggest()
        self.resize(520, 0)
        self.build: Build | None = None

    def _suggest(self) -> None:
        kind = self.kind_box.currentData()
        self.retarget_box.setVisible(kind == "merge-v")
        base = {"merge-v": "view", "merge-p": "player_held", "merge-w": "world",
                "merge-players": "players", "merge-zhands": "zombie_hands"}[kind]
        category = self.category_box.currentData() if hasattr(self, "category_box") else None
        if category:
            base = f"{re.sub(r'[^0-9A-Za-z]+', '_', category).strip('_').lower()}_{base}"
        name, n = base, 2
        while name in self.project.builds:
            name, n = f"{base}_{n}", n + 1
        self.name_edit.setText(name)

    def _accept(self) -> None:
        name = self.name_edit.text().strip()
        if not name or any(c in name for c in '/\\:*?"<>|'):
            QMessageBox.warning(self, "New build", "Use a plain folder name.")
            return
        kind = self.kind_box.currentData()
        self.build = Build(name=name, kind=kind, category=self.category_box.currentData(),
                           retarget=kind == "merge-v" and self.retarget_box.isChecked())
        try:
            self.project.add_build(self.build)
        except (ProjectError, ValueError) as exc:
            QMessageBox.warning(self, "New build", str(exc))
            return
        self.accept()


class BuildPanel(QTabWidget):
    """Settings / Results / Manifest / Outputs of one build."""

    run_requested = Signal(str)
    compile_requested = Signal(str)
    preview_requested = Signal(str)  # absolute QC path
    preview_body_requested = Signal(str, int, int)  # QC path, pev_body, sequence (-1)
    open_requested = Signal(str)  # absolute path (folder or .mdl)
    changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project: Project | None = None
        self.build_name = ""

        # -- settings
        self.settings_page = QWidget()
        self.settings_layout = QVBoxLayout(self.settings_page)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.settings_page)
        self.addTab(scroll, "Settings")
        # -- results
        results = QWidget()
        layout = QVBoxLayout(results)
        self.summary = QLabel("not run yet")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.budgets = _table(["Part", "Bones", "Bodyparts", "Submodels", "Sequences",
                               "Textures", "Max seq bytes"])
        self.gates = _table(["Part", "Check", "Result", "Detail"])
        self.failures = QLabel("")
        self.failures.setWordWrap(True)
        self.failures.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(QLabel("Budgets (red = over the limit)"))
        layout.addWidget(self.budgets, 1)
        layout.addWidget(QLabel("Verification gates"))
        layout.addWidget(self.gates, 2)
        layout.addWidget(self.failures)
        self.addTab(results, "Results")
        # -- manifest
        self.manifest = QTableWidget()
        self.manifest.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.manifest.verticalHeader().setVisible(False)
        self.manifest.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.manifest.setToolTip("double-click: show this asset in the merged model "
                                 "(its pev_body and first animation)")
        self.manifest.cellDoubleClicked.connect(self._preview_row)
        self.addTab(self.manifest, "Manifest")
        # -- outputs
        outputs = QWidget()
        out_layout = QVBoxLayout(outputs)
        self.outputs = QListWidget()
        self.outputs.itemDoubleClicked.connect(lambda _i: self._preview())
        out_layout.addWidget(self.outputs, 1)
        row = QHBoxLayout()
        for text, slot in (("Preview in viewport", self._preview),
                           ("Open .mdl in viewer", self._open_mdl),
                           ("Show folder", self._show_folder)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        out_layout.addLayout(row)
        self.addTab(outputs, "Outputs")

    # -- binding -----------------------------------------------------------
    def show_build(self, project: Project | None, name: str) -> None:
        self.project, self.build_name = project, name
        self._build_settings()
        self.refresh_results()

    def _clear_settings(self) -> None:
        while self.settings_layout.count():
            item = self.settings_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def _build_settings(self) -> None:
        self._clear_settings()
        project = self.project
        if project is None or self.build_name not in project.builds:
            return
        build = project.builds[self.build_name]
        title = QLabel(f"<b>{build.name}</b> · {build.kind}")
        self.settings_layout.addWidget(title)

        assets_box = QGroupBox("Assets")
        assets_layout = QVBoxLayout(assets_box)
        self.all_radio = QRadioButton("every asset of the accepted kinds")
        self.category_radio = QRadioButton("every asset of the accepted kinds in category")
        self.pick_radio = QRadioButton("only the checked assets")
        self.category_combo = QComboBox()
        for category in sorted(project.categories, key=str.lower):
            self.category_combo.addItem(category)
        if build.category:
            if self.category_combo.findText(build.category) < 0:
                self.category_combo.addItem(build.category)
            self.category_combo.setCurrentText(build.category)
        by_category = build.category is not None and not build.assets
        self.all_radio.setChecked(not build.assets and not by_category)
        self.category_radio.setChecked(by_category)
        self.pick_radio.setChecked(bool(build.assets))
        self.category_radio.setEnabled(self.category_combo.count() > 0)
        self.category_combo.setEnabled(by_category)
        self.category_radio.toggled.connect(self.category_combo.setEnabled)
        self.asset_list = _asset_list(project, build.kind, build.assets)
        self.asset_list.setEnabled(bool(build.assets))
        self.pick_radio.toggled.connect(self.asset_list.setEnabled)
        category_row = QHBoxLayout()
        category_row.addWidget(self.category_radio)
        category_row.addWidget(self.category_combo, 1)
        assets_layout.addWidget(self.all_radio)
        assets_layout.addLayout(category_row)
        assets_layout.addWidget(self.pick_radio)
        assets_layout.addWidget(self.asset_list)
        self.settings_layout.addWidget(assets_box)

        self.retarget_check: QCheckBox | None = None
        self.retarget_form: OptionsForm | None = None
        if build.kind == "merge-v":
            self.retarget_check = QCheckBox("put every asset on our hands first, then merge "
                                            "with shared hands (retargeted ones kept as is)")
            self.retarget_check.setChecked(build.retarget)
            self.settings_layout.addWidget(self.retarget_check)
            retarget_box = QGroupBox("Retarget options")
            retarget_layout = QVBoxLayout(retarget_box)
            self.retarget_form = OptionsForm(RetargetOptions, "retarget",
                                             build.retarget_options)
            retarget_layout.addWidget(self.retarget_form)
            retarget_box.setVisible(build.retarget)
            self.retarget_check.toggled.connect(retarget_box.setVisible)
            self.settings_layout.addWidget(retarget_box)

        options_box = QGroupBox(f"{build.kind} options")
        options_layout = QVBoxLayout(options_box)
        self.options_form = OptionsForm(BUILD_KINDS[build.kind].options, build.kind,
                                        build.options)
        options_layout.addWidget(self.options_form)
        self.settings_layout.addWidget(options_box)

        row = QHBoxLayout()
        save = QPushButton("Save")
        save.clicked.connect(self.save)
        run = QPushButton("Save && Run")
        run.clicked.connect(lambda: self.save() and self.run_requested.emit(self.build_name))
        compile_button = QPushButton("Compile")
        compile_button.clicked.connect(lambda: self.compile_requested.emit(self.build_name))
        for button in (save, run, compile_button):
            row.addWidget(button)
        holder = QWidget()
        holder.setLayout(row)
        self.settings_layout.addWidget(holder)
        self.settings_layout.addStretch(1)

    def edited_build(self) -> Build:
        """The build as currently shown in the form (raises ValueError)."""
        assert self.project is not None
        current = self.project.builds[self.build_name]
        return Build(
            name=current.name, kind=current.kind,
            assets=_checked(self.asset_list) if self.pick_radio.isChecked() else [],
            category=(self.category_combo.currentText() or None)
            if self.category_radio.isChecked() else None,
            options=self.options_form.values(),
            retarget=bool(self.retarget_check and self.retarget_check.isChecked()),
            retarget_options=(self.retarget_form.values()
                              if self.retarget_form is not None else {}),
        )

    def save(self) -> bool:
        if self.project is None:
            return False
        try:
            build = self.edited_build()
            if self.pick_radio.isChecked() and not build.assets:
                raise ValueError("check at least one asset")
            self.project.update_build(build)
        except (ProjectError, ValueError) as exc:
            QMessageBox.warning(self, "Build settings", str(exc))
            return False
        self.changed.emit(self.build_name)
        return True

    # -- results -----------------------------------------------------------
    def refresh_results(self) -> None:
        project = self.project
        self.outputs.clear()
        self.manifest.clear()
        self.manifest.setRowCount(0)
        self.manifest.setColumnCount(0)
        if project is None or self.build_name not in project.builds:
            self.summary.setText("")
            self.budgets.setRowCount(0)
            self.gates.setRowCount(0)
            return
        base = project.build_dir(self.build_name)
        record = load_record(base / "last_run.json")
        if record is None:
            self.summary.setText("not run yet")
            self.budgets.setRowCount(0)
            self.gates.setRowCount(0)
            self.failures.setText("")
            return
        ok = record["exit_code"] == 0
        self.summary.setText(
            f"<b style='color:{(OK_COLOR if ok else BAD_COLOR).name()}'>"
            f"{'OK' if ok else 'FAILED'}</b> · exit {record['exit_code']} · "
            f"{len(record['outputs'])} part(s) · {record['seconds']} s")
        parts: list[dict[str, Any]] = record.get("parts", [])
        _fill(self.budgets, [[p["name"], p["bones"], p["bodyparts"], p["submodels"],
                              p["sequences"], p["textures"],
                              f"{p['seq_bytes']} ({p['seq_bytes_name']})"]
                             for p in parts])
        keys = ["bones", "bodyparts", "submodels", "sequences", "textures", "seq_bytes"]
        for r, part in enumerate(parts):
            for c, key in enumerate(keys, start=1):
                over = part[key] > LIMITS[key]
                item = self.budgets.item(r, c)
                item.setForeground(QBrush(BAD_COLOR if over else self.palette().text().color()))
                item.setToolTip(f"limit {LIMITS[key]}")
        _fill(self.gates, [[g["part"], g["check"], "PASS" if g["passed"] else "FAIL",
                            g["detail"]] for g in record.get("gates", [])])
        for r, gate in enumerate(record.get("gates", [])):
            self.gates.item(r, 2).setForeground(QBrush(OK_COLOR if gate["passed"]
                                                       else BAD_COLOR))
        failures = record.get("failures", [])
        warnings = [w for w in record.get("warnings", []) if w not in failures]
        text = ("Failures:\n" + "\n".join(f"• {f}" for f in failures)) if failures else ""
        if warnings:
            text += ("\n" if text else "") + "Warnings:\n" + "\n".join(f"• {w}"
                                                                       for w in warnings)
        self.failures.setText(text)
        header, rows = manifest_rows(base / "output")
        self.manifest.setColumnCount(len(header))
        self.manifest.setHorizontalHeaderLabels(header)
        self.manifest.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                self.manifest.setItem(r, c, QTableWidgetItem(value))
        self.manifest.resizeColumnsToContents()
        for relative in record["outputs"]:
            qc = project.root / relative
            mdl = qc.with_suffix(".mdl")
            label = relative + ("   ✓ compiled" if mdl.exists() else "")
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, str(qc))
            self.outputs.addItem(item)
        if self.outputs.count():
            self.outputs.setCurrentRow(0)

    def _preview_row(self, row: int, _column: int = 0) -> None:
        header = [self.manifest.horizontalHeaderItem(c).text()
                  for c in range(self.manifest.columnCount())]
        cells = {h: (self.manifest.item(row, c).text() if self.manifest.item(row, c) else "")
                 for c, h in enumerate(header)}
        qcs = [Path(self.outputs.item(i).data(Qt.ItemDataRole.UserRole))
               for i in range(self.outputs.count())]
        if not qcs or not cells.get("pev_body", "").isdigit():
            return
        qc = qcs[0]
        if cells.get("model"):
            stem = Path(cells["model"]).stem
            qc = next((q for q in qcs if q.stem == stem), qc)
        anims = [h for h in header if h.startswith("anim_") and cells.get(h, "").isdigit()]
        first = next((h for h in anims if h == "anim_idle"), anims[0] if anims else "")
        self.preview_body_requested.emit(str(qc), int(cells["pev_body"]),
                                         int(cells[first]) if first else -1)

    def _current_qc(self) -> Path | None:
        item = self.outputs.currentItem()
        return Path(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def _preview(self) -> None:
        qc = self._current_qc()
        if qc is not None:
            self.preview_requested.emit(str(qc))

    def _open_mdl(self) -> None:
        qc = self._current_qc()
        if qc is not None:
            self.open_requested.emit(str(qc.with_suffix(".mdl")))

    def _show_folder(self) -> None:
        qc = self._current_qc()
        if qc is not None:
            self.open_requested.emit(str(qc.parent))
        elif self.project is not None and self.build_name:
            self.open_requested.emit(str(self.project.build_dir(self.build_name)))


__all__ = ["BuildPanel", "NewBuildDialog"]
