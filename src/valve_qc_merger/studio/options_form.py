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

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

# Set by the build (where the staged inputs and the output go).
HIDDEN = {"models_dir", "out", "weapon_dir", "source", "qc"}
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
    def __init__(self, options_type: type, command_name: str,
                 values: dict[str, Any] | None = None,
                 parent: QWidget | None = None, *,
                 exclude: frozenset[str] = frozenset()) -> None:
        super().__init__(parent)
        self.options_type = options_type
        self._fields = [f for f in dataclasses.fields(options_type)
                        if f.name not in HIDDEN and f.name not in exclude]
        self._widgets: dict[str, QWidget] = {}
        helps = cli_help(command_name)
        self._choices = cli_choices(command_name)
        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        values = values or {}
        for f in self._fields:
            annotation = str(f.type).replace(" ", "")
            value = values.get(f.name, _default(f))
            if f.name in self._choices:
                widget: QWidget = QComboBox()
                widget.addItems(self._choices[f.name])
                widget.setCurrentText(str(value))
            else:
                widget = self._make(annotation, value, LIST_SEPARATORS.get(f.name, ","))
            widget.setToolTip(helps.get(f.name, ""))
            self._widgets[f.name] = widget
            form.addRow(f.name.replace("_", " "), widget)

    def _make(self, annotation: str, value: Any, separator: str = ",") -> QWidget:
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
            edit.setPlaceholderText("comma-separated" if separator == ","
                                    else f"'{separator}'-separated")
        else:
            edit.setText("" if value is None else str(value))
            if "|None" in annotation:
                edit.setPlaceholderText("default")
        if base == "Path":
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(edit, 1)
            browse = QPushButton("…")
            browse.setFixedWidth(28)
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
            default = _default(f)
            if value != default:
                out[f.name] = str(value) if isinstance(value, Path) else value
        return out


__all__ = ["HIDDEN", "OptionsForm", "cli_help"]
