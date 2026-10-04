"""A form generated from a service's options dataclass.

Widgets follow the field type (bool, int, float, str, Path, list[str] and
their ``| None`` forms); tooltips come from the matching CLI flag's help, so
the GUI and ``--help`` never disagree. Only values that differ from the
dataclass default are returned, keeping ``project.toml`` minimal.
"""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.studio import theme
from valve_qc_merger.studio.field_specs import default_label, spec_for
from valve_qc_merger.studio.icons import icon

# Set by the build (where the staged inputs and the output go).
HIDDEN = {"models_dir", "out", "weapon_dir", "source", "qc"}
# what a number field accepts while typing (decimal point, never a comma)
_NUM = r"-?\d*(?:\.\d*)?"
NUMBER_PATTERNS = {"int": r"-?\d*", "float": _NUM,
                   "list[float]": rf"\s*{_NUM}(?:\s*,\s*{_NUM})*\s*,?\s*"}
# list fields whose entries contain commas themselves ("left:0,0,-0.4")
LIST_SEPARATORS = {"grip_offset": ";"}


def _cli_actions(command_name: str) -> list[argparse.Action]:
    from valve_qc_merger.commands import iter_commands
    for command in iter_commands():
        if command.name == command_name:
            parser = argparse.ArgumentParser(add_help=False)
            command.configure(parser)
            return list(parser._actions)
    return []


def cli_help(command_name: str) -> dict[str, str]:
    """``dest -> help`` of a CLI command's flags."""
    return {a.dest: a.help or "" for a in _cli_actions(command_name) if a.help}


def cli_choices(command_name: str) -> dict[str, list[str]]:
    """``dest -> allowed values`` of a CLI command's ``choices=`` flags."""
    return {a.dest: [str(c) for c in a.choices] for a in _cli_actions(command_name)
            if a.choices}


def _default(f: dataclasses.Field) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    return None


class OptionsForm(QWidget):
    """Basic options on top, the rest under a collapsible "Advanced options"
    (open when one of them is set); labels, tooltips and placeholders from
    :mod:`.field_specs`, else the field name and the CLI help."""

    def __init__(self, options_type: type, command_name: str,
                 values: dict[str, Any] | None = None,
                 parent: QWidget | None = None, *,
                 exclude: frozenset[str] = frozenset()) -> None:
        super().__init__(parent)
        self.options_type = options_type
        values = values or {}
        candidates = [f for f in dataclasses.fields(options_type)
                      if f.name not in HIDDEN and f.name not in exclude]
        self._specs = {f.name: spec_for(command_name, f.name) for f in candidates}
        hidden = {n for n, spec in self._specs.items() if spec is not None and spec.hidden}
        self._fields = [f for f in candidates if f.name not in hidden]
        # values the form does not show are handed back untouched
        self._kept = {k: v for k, v in values.items() if k in hidden}
        self._widgets: dict[str, QWidget] = {}
        helps = cli_help(command_name)
        self._choices = cli_choices(command_name)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        basic = self._form()
        outer.addLayout(basic)
        advanced_fields = [f for f in self._fields
                           if (spec := self._specs[f.name]) is not None and spec.advanced]
        if len(advanced_fields) == len(self._fields):
            advanced_fields = []  # nothing basic: no point hiding everything
        self.advanced_toggle: QToolButton | None = None
        self.advanced_box: QWidget | None = None
        advanced = basic
        if advanced_fields:
            self.advanced_toggle = QToolButton()
            self.advanced_toggle.setCheckable(True)
            self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self.advanced_toggle.setText(f"Advanced options ({len(advanced_fields)})")
            self.advanced_toggle.setAutoRaise(True)
            self.advanced_box = QWidget()
            advanced = self._form()
            advanced.setContentsMargins(0, 0, 0, 0)
            self.advanced_box.setLayout(advanced)
            outer.addWidget(self.advanced_toggle, 0, Qt.AlignmentFlag.AlignLeft)
            outer.addWidget(self.advanced_box)
            self.advanced_toggle.toggled.connect(self._show_advanced)
        for f in self._fields:
            spec = self._specs[f.name]
            annotation = str(f.type).replace(" ", "")
            value = values.get(f.name, _default(f))
            if f.name in self._choices:
                widget: QWidget = QComboBox()
                widget.addItems(self._choices[f.name])
                widget.setCurrentText(str(value))
            else:
                widget = self._make(annotation, value, LIST_SEPARATORS.get(f.name, ","),
                                    placeholder=spec.placeholder if spec else "")
            if spec is not None and spec.invert and isinstance(widget, QCheckBox):
                widget.setChecked(not bool(value))
            tip = spec.help if spec is not None and spec.help else helps.get(f.name, "")
            widget.setToolTip(tip)
            label = QLabel(spec.label if spec is not None else default_label(f.name))
            label.setToolTip(tip)
            self._widgets[f.name] = widget
            target = advanced if f in advanced_fields else basic
            target.addRow(label, widget)
        if self.advanced_toggle is not None:
            changed = set(self.values()) & {f.name for f in advanced_fields}
            self.advanced_toggle.setChecked(bool(changed))
            self._show_advanced(bool(changed))

    @staticmethod
    def _form() -> QFormLayout:
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        return form

    def _show_advanced(self, shown: bool) -> None:
        if self.advanced_toggle is None or self.advanced_box is None:
            return
        self.advanced_toggle.setIcon(icon("chevron-down" if shown else "chevron-right",
                                          theme.TOKENS["muted"]))
        self.advanced_box.setVisible(shown)

    def _make(self, annotation: str, value: Any, separator: str = ",", *,
              placeholder: str = "") -> QWidget:
        base = annotation.replace("|None", "")
        if base == "bool":
            box = QCheckBox()
            box.setChecked(bool(value))
            return box
        if base == "int" and "|None" not in annotation:
            spin = QSpinBox()
            spin.setRange(-1_000_000, 1_000_000)
            spin.setValue(int(value or 0))
            return spin
        if base == "float" and "|None" not in annotation:
            spin = QDoubleSpinBox()
            spin.setRange(-1e6, 1e6)
            spin.setDecimals(3)
            spin.setValue(float(value or 0.0))
            return spin
        edit = QLineEdit()
        if base.startswith("list["):
            edit.setText(f"{separator} ".join(str(v) for v in value or []))
            edit.setPlaceholderText(placeholder or ("comma-separated" if separator == ","
                                                    else f"'{separator}'-separated"))
        else:
            edit.setText("" if value is None else str(value))
            if "|None" in annotation:
                edit.setPlaceholderText(placeholder or "default")
        number = NUMBER_PATTERNS.get(base)
        if number is not None:  # typing letters into a number field is refused
            edit.setValidator(QRegularExpressionValidator(QRegularExpression(number), edit))
        if base == "Path":
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(edit, 1)
            browse = QToolButton()
            browse.setIcon(icon("folder-open"))
            browse.setToolTip("Choose a file…")
            browse.clicked.connect(lambda: self._browse(edit))
            layout.addWidget(browse)
            row.edit = edit  # type: ignore[attr-defined]
            return row
        return edit

    def _browse(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose file", edit.text())
        if path:
            edit.setText(path)

    def values(self) -> dict[str, Any]:
        """Field values that differ from the dataclass defaults (raises
        ValueError on an unparsable number)."""
        out: dict[str, Any] = {}
        for f in self._fields:
            widget = self._widgets[f.name]
            annotation = str(f.type).replace(" ", "")
            base = annotation.replace("|None", "")
            if isinstance(widget, QComboBox):
                value: Any = widget.currentText()
            elif isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                value = widget.value()
            else:
                edit = getattr(widget, "edit", widget)
                text = edit.text().strip()
                if base == "list[float]":
                    value = [float(part) for part in text.split(",") if part.strip()]
                elif base.startswith("list["):
                    separator = LIST_SEPARATORS.get(f.name, ",")
                    value = [part.strip() for part in text.split(separator) if part.strip()]
                elif not text:
                    value = None
                elif base == "int":
                    value = int(text)
                elif base == "float":
                    value = float(text)
                elif base == "Path":
                    value = Path(text)
                else:
                    value = text
            spec = self._specs.get(f.name)
            if spec is not None and spec.invert and isinstance(value, bool):
                value = not value
            default = _default(f)
            if value != default:
                out[f.name] = str(value) if isinstance(value, Path) else value
        return {**out, **self._kept}


__all__ = ["HIDDEN", "OptionsForm", "cli_help"]
