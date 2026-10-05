"""Build editing and results: the right-hand panel when a build is selected."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
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
from valve_qc_merger.project.model import DEFAULT_DEPLOY_DIR, DEPLOY_DIRS
from valve_qc_merger.services.retarget import RetargetOptions
from valve_qc_merger.studio import theme
from valve_qc_merger.studio.build_report import LIMITS, load_record, manifest_rows
from valve_qc_merger.studio.icons import icon
from valve_qc_merger.studio.options_form import OptionsForm
from valve_qc_merger.studio.widgets import KIND_TITLES, _fill, _table

OK_COLOR = theme.color("success")
BAD_COLOR = theme.color("danger")


# one line per build kind: what the merge makes
KIND_DESCRIPTIONS = {
    "merge-v": "First-person weapons → one v_ model; each weapon is a bodygroup entry the "
               "server picks with pev->body.",
    "merge-p": "Weapons in the player's hands → one p_ model with a bodygroup per weapon.",
    "merge-w": "Weapons lying on the ground → one w_ model with a bodygroup per weapon.",
    "merge-players": "Player characters → models on one shared rig, a skin per character.",
    "merge-zhands": "Zombie knife and grenade hands → one model sharing a single grenade.",
}


def _delete_layout(layout) -> None:  # noqa: ANN001 - QLayout
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            item.widget().deleteLater()
        elif item.layout() is not None:
            _delete_layout(item.layout())


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



def _scrolled(label: QLabel, height: int = 150) -> QWidget:
    """``label`` in a scroll area at most ``height`` px tall, hidden while
    empty: a long list (60 rejected models) must not stretch the window."""
    from PySide6.QtWidgets import QFrame, QScrollArea
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setMaximumHeight(height)
    area.setWidget(label)
    label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    original = label.setText

    def set_text(text: str) -> None:
        original(text)
        area.setVisible(bool(text))
    label.setText = set_text  # type: ignore[method-assign]
    area.setVisible(bool(label.text()))
    return area

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
            self.kind_box.addItem(f"{titles}  ·  {count} asset{'s' * (count != 1)}", kind)
        self.kind_box.currentIndexChanged.connect(self._suggest)
        self.kind_hint = QLabel()
        theme.set_role(self.kind_hint, "hint")
        self.kind_hint.setWordWrap(True)
        self.retarget_box = QCheckBox("Put every model on our hands first")
        self.retarget_box.setToolTip("Models not yet on our hands are retargeted before the "
                                     "merge (Retarget results are taken as they are); the "
                                     "merge then shares one hands bodygroup.")
        self.category_box = QComboBox()
        self.category_box.addItem("Every category", None)
        for category in sorted(project.categories, key=str.lower):
            self.category_box.addItem(category, category)
        self.category_box.currentIndexChanged.connect(self._suggest)
        form = QFormLayout(self)
        form.setContentsMargins(20, 18, 20, 16)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        heading = QLabel("New build")
        theme.set_role(heading, "heading")
        form.addRow(heading)
        form.addRow("Merge", self.kind_box)
        form.addRow("", self.kind_hint)
        form.addRow("Assets from", self.category_box)
        form.addRow("Name", self.name_edit)
        form.addRow("", self.retarget_box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        best = max(range(self.kind_box.count()), key=lambda i: sum(
            1 for a in project.assets.values()
            if a.kind in BUILD_KINDS[self.kind_box.itemData(i)].asset_kinds))
        self.kind_box.setCurrentIndex(best)
        self._suggest()
        # tall enough for the wrapped kind description at this width
        height = self.layout().totalHeightForWidth(560)
        self.resize(560, height if height > 0 else self.sizeHint().height())
        self.build: Build | None = None

    def _suggest(self) -> None:
        kind = self.kind_box.currentData()
        self.retarget_box.setVisible(kind == "merge-v")
        self.kind_hint.setText(KIND_DESCRIPTIONS.get(kind, ""))
        self.kind_hint.setMinimumHeight(self.kind_hint.heightForWidth(400))
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
    plan_requested = Signal(str)
    deploy_requested = Signal(str)
    compile_requested = Signal(str)
    preview_requested = Signal(str)  # absolute QC path
    preview_body_requested = Signal(str, int, int)  # QC path, pev_body, sequence (-1)
    open_requested = Signal(str)  # absolute path (folder or .mdl)
    asset_requested = Signal(str)  # select this asset (from a plan row)
    changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project: Project | None = None
        self.build_name = ""

        # -- settings: the form scrolls, the actions stay in a footer below it
        self.settings_page = QWidget()
        self.settings_layout = QVBoxLayout(self.settings_page)
        self.settings_layout.setContentsMargins(12, 12, 12, 12)
        self.settings_layout.setSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.settings_page)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_tab = QWidget()
        tab_layout = QVBoxLayout(settings_tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(0)
        tab_layout.addWidget(scroll, 1)
        tab_layout.addWidget(self._settings_footer())
        self.addTab(settings_tab, "Settings")
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
        layout.addWidget(_scrolled(self.failures))
        self.addTab(results, "Results")
        # -- plan
        plan = QWidget()
        plan_layout = QVBoxLayout(plan)
        self.plan_summary = QLabel("Plan shows the parts a run would make — who goes "
                                   "where, pev_body, who is rejected — without merging.")
        self.plan_summary.setWordWrap(True)
        self.plan_table = _table(["Part", "pev_body", "Model", "Note"])
        self.plan_table.setToolTip("Double-click: select the asset")
        self.plan_table.cellDoubleClicked.connect(self._plan_row_activated)
        self.plan_rejected = QLabel("")
        self.plan_rejected.setWordWrap(True)
        self.plan_rejected.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        plan_layout.addWidget(self.plan_summary)
        plan_layout.addWidget(self.plan_table, 1)
        plan_layout.addWidget(_scrolled(self.plan_rejected))
        self.plan_tab = self.addTab(plan, "Plan")
        # -- manifest
        self.manifest = QTableWidget()
        self.manifest.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.manifest.verticalHeader().setVisible(False)
        self.manifest.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.manifest.setToolTip("Double-click: show this asset in the merged model "
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
    def _settings_footer(self) -> QWidget:
        footer = QWidget()
        theme.set_role(footer, "footer")
        footer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(footer)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(6)

        def button(text: str, name: str, tip: str, slot, primary: bool = False) -> QPushButton:  # noqa: ANN001
            out = QPushButton(icon(name, theme.TOKENS["on_accent"] if primary else None), text)
            out.setToolTip(tip)
            out.clicked.connect(slot)
            if primary:
                theme.set_primary(out)
            return out

        self.save_button = button("Save", "check", "Save the settings", self.save)
        self.plan_button = button(
            "Plan", "list-checks", "Save, then show what a run would make (parts, pev_body, "
            "rejections) without merging",
            lambda: self.save() and self.plan_requested.emit(self.build_name))
        self.compile_button = button("Compile", "hammer", "Compile the last run's output",
                                     lambda: self.compile_requested.emit(self.build_name))
        self.deploy_button = button(
            "Deploy", "rocket", "Save, then copy the compiled models + manifest into the "
            "game folder", lambda: self.save() and self.deploy_requested.emit(self.build_name))
        self.run_button = button("Run", "play", "Save, then merge",
                                 lambda: self.save() and self.run_requested.emit(self.build_name),
                                 primary=True)
        row.addWidget(self.save_button)
        row.addStretch(1)
        for widget in (self.plan_button, self.compile_button, self.deploy_button,
                       self.run_button):
            row.addWidget(widget)
        self.settings_footer = footer
        footer.setVisible(False)
        return footer

    def show_build(self, project: Project | None, name: str) -> None:
        self.project, self.build_name = project, name
        self._build_settings()
        self.refresh_results()
        self.refresh_plan()

    def _clear_settings(self) -> None:
        while self.settings_layout.count():
            item = self.settings_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
            elif item.layout() is not None:
                _delete_layout(item.layout())

    def _section(self, title: str) -> None:
        label = QLabel(title.upper())
        theme.set_role(label, "section")
        label.setContentsMargins(0, 10, 0, 0)
        self.settings_layout.addWidget(label)

    def _build_settings(self) -> None:
        self._clear_settings()
        project = self.project
        self.settings_footer.setVisible(project is not None
                                        and self.build_name in project.builds)
        if project is None or self.build_name not in project.builds:
            return
        build = project.builds[self.build_name]
        spec = BUILD_KINDS[build.kind]
        kinds = ", ".join(KIND_TITLES[k] for k in sorted(spec.asset_kinds))
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        title = QLabel(build.name)
        theme.set_role(title, "heading")
        head.addWidget(title)
        badge = QLabel(build.kind)
        theme.set_role(badge, "kbd")
        head.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        holder = QWidget()
        holder.setLayout(head)
        self.settings_layout.addWidget(holder)
        about = QLabel(KIND_DESCRIPTIONS.get(build.kind, ""))
        theme.set_role(about, "hint")
        about.setWordWrap(True)
        self.settings_layout.addWidget(about)

        self._section("Assets")
        self.all_radio = QRadioButton(f"All {kinds}")
        self.category_radio = QRadioButton("Only the category")
        self.pick_radio = QRadioButton("Only the checked assets")
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
        self.asset_list.setVisible(bool(build.assets))
        self.asset_list.setMaximumHeight(180)
        self.pick_radio.toggled.connect(self.asset_list.setVisible)
        category_row = QHBoxLayout()
        category_row.setContentsMargins(0, 0, 0, 0)
        category_row.addWidget(self.category_radio)
        category_row.addWidget(self.category_combo, 1)
        category_holder = QWidget()
        category_holder.setLayout(category_row)
        for widget in (self.all_radio, category_holder, self.pick_radio, self.asset_list):
            self.settings_layout.addWidget(widget)

        self.retarget_check: QCheckBox | None = None
        self.retarget_form: OptionsForm | None = None
        if build.kind == "merge-v":
            self._section("Hands")
            self.retarget_check = QCheckBox("Put every model on our hands first")
            self.retarget_check.setChecked(build.retarget)
            self.settings_layout.addWidget(self.retarget_check)
            hint = QLabel("Models not yet on our hands are retargeted with the options "
                          "below; ones made by Retarget or already wearing our hands are "
                          "taken as they are. The merge then uses one shared hands "
                          "bodygroup.")
            theme.set_role(hint, "hint")
            hint.setWordWrap(True)
            hint.setContentsMargins(24, 0, 0, 0)
            self.settings_layout.addWidget(hint)
            self.retarget_form = OptionsForm(RetargetOptions, "retarget",
                                             build.retarget_options)
            self.retarget_form.setContentsMargins(24, 4, 0, 0)
            self.retarget_form.setVisible(build.retarget)
            self.retarget_check.toggled.connect(self.retarget_form.setVisible)
            self.settings_layout.addWidget(self.retarget_form)

        self._section("Merge options")
        self.options_form = OptionsForm(spec.options, build.kind, build.options)
        self.settings_layout.addWidget(self.options_form)
        shared = self.options_form._widgets.get("shared_hands")
        if self.retarget_check is not None and shared is not None:
            # "on our hands first" always merges with shared hands
            shared.setEnabled(not build.retarget)
            self.retarget_check.toggled.connect(lambda on: shared.setEnabled(not on))

        self._section("Deploy")
        deploy_row = QFormLayout()
        deploy_row.setContentsMargins(0, 0, 0, 0)
        self.deploy_edit = QLineEdit(build.deploy_dir or "")
        self.deploy_edit.setPlaceholderText(DEPLOY_DIRS.get(build.kind, DEFAULT_DEPLOY_DIR))
        tip = ("Where Deploy copies the compiled models and the manifest, inside the game "
               "folder (Project ▸ Settings).")
        self.deploy_edit.setToolTip(tip)
        deploy_label = QLabel("Folder in the game")
        deploy_label.setToolTip(tip)
        deploy_row.addRow(deploy_label, self.deploy_edit)
        deploy_holder = QWidget()
        deploy_holder.setLayout(deploy_row)
        self.settings_layout.addWidget(deploy_holder)
        self.settings_layout.addStretch(1)
        self.plan_button.setVisible(build.kind in Project.PLANNABLE)

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
            deploy_dir=self.deploy_edit.text().strip().strip("/\\") or None,
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

    # -- plan ----------------------------------------------------------------
    def refresh_plan(self) -> None:
        project = self.project
        self.plan_table.setRowCount(0)
        self.plan_rejected.setText("")
        if project is None or self.build_name not in project.builds:
            return
        record = load_record(project.build_dir(self.build_name) / "plan.json")
        if record is None:
            self.plan_summary.setText("Not planned yet: Save & Plan (merge-v builds).")
            return
        parts = record.get("parts", [])
        models = sum(len(p["models"]) for p in parts)
        failures = record.get("failures", [])
        self.plan_summary.setText(
            f"<b>{len(parts)} part(s)</b> · {models} model(s) · "
            f"{len(failures)} rejected · planned in {record.get('seconds', '?')} s")
        rows: list[list[object]] = []
        for part in parts:
            bodies = part.get("pev_body", {})
            folded = set(part.get("folded", []))
            for model in part["models"]:
                note = "parts folded into one submodel" if model in folded else ""
                rows.append([part["part"], bodies.get(model, "after run"), model, note])
        _fill(self.plan_table, rows)
        warnings = [w for w in record.get("warnings", []) if w not in failures]
        text = "\n".join(f"✗ {f}" for f in failures)
        if warnings:
            text += ("\n" if text else "") + "\n".join(f"• {w}" for w in warnings)
        self.plan_rejected.setText(text)

    def _plan_row_activated(self, row: int, _column: int) -> None:
        item = self.plan_table.item(row, 2)
        if item is not None:
            self.asset_requested.emit(item.text())

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
                item.setToolTip(f"Limit {LIMITS[key]}")
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
