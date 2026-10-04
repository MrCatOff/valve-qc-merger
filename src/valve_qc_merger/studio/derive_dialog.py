"""The Retarget dialog: make a new asset from one (or several) by swapping
the hands, or by bringing the bones to the canonical rig.

Offsets are the grip-tuning knobs of the hand swap: the *weapon offset*
moves the weapon relative to both hands (model space, at the grip frame);
the per-hand *grip offsets* shift one palm in its own axes. After an offset
the fingers re-snug to the weapon, so nudging and re-running is the way to
find a better grip.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.handswap import tuning
from valve_qc_merger.project import DERIVE_MODES
from valve_qc_merger.resources import resource_path
from valve_qc_merger.services.canonicalize import CanonicalizeOptions
from valve_qc_merger.services.retarget import RetargetOptions
from valve_qc_merger.services.zhands_grenade import ZhandsGrenadeOptions
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio.options_form import OptionsForm

# fields of RetargetOptions the dialog shows as spin boxes, or never
_OFFSET_FIELDS = frozenset({"weapon_offset", "grip_offset", "curl"})
# mode -> (card icon, what it does)
MODE_CARDS = {
    "hands": ("hand", "Our male/female hands replace the model's own; every animation is "
                      "retargeted onto them. Ready for a shared-hands merge."),
    "canon": ("bone", "The model keeps its own hands; its bones are renamed and "
                      "reparented onto the canonical rig (Bip01 root, no Nubs)."),
    "grenade": ("bomb", "Zombie hands without a grenade: these hands are put on the "
                        "bundled frog bomb with its idle, pull-pin, throw and draw."),
}
FINGER_TITLES = {"BigFinger": "Thumb", "ForeFinger": "Index", "MiddleFinger": "Middle",
                 "RingFinger": "Ring", "PinkyFinger": "Pinky"}
_RETARGET_FIXED = frozenset({"category", "compile", "studiomdl", "modelname"})
SIDES = ("left", "right")


class Vec3Edit(QWidget):
    """Three spin boxes (x, y, z)."""

    def __init__(self, values: list[float] | None = None, *, step: float = 0.1,
                 tooltip: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.spins: list[QDoubleSpinBox] = []
        for axis, value in zip("xyz", values or [0.0, 0.0, 0.0], strict=False):
            spin = QDoubleSpinBox()
            spin.setRange(-50.0, 50.0)
            spin.setDecimals(2)
            spin.setSingleStep(step)
            spin.setPrefix(f"{axis} ")
            kit.number_spin(spin)
            spin.setValue(float(value))
            spin.setToolTip(tooltip)
            layout.addWidget(spin)
            self.spins.append(spin)

    def value(self) -> list[float]:
        return [round(s.value(), 4) for s in self.spins]

    def set_value(self, values: list[float]) -> None:
        for spin, value in zip(self.spins, values, strict=False):
            spin.setValue(float(value))


def parse_grip_offsets(specs: list[str]) -> dict[str, list[float]]:
    """``["left:0,0,-0.4"]`` -> ``{"left": [0, 0, -0.4]}`` (bad entries dropped)."""
    out: dict[str, list[float]] = {}
    for spec in specs or []:
        side, _, xyz = str(spec).partition(":")
        try:
            values = [float(v) for v in xyz.split(",")]
        except ValueError:
            continue
        if side.strip().lower() in SIDES and len(values) == 3:
            out[side.strip().lower()] = values
    return out


def parse_curls(specs: list[str]) -> dict[str, dict[str, float]]:
    """``["left:ForeFinger:8"]`` -> ``{"left": {"ForeFinger": 8.0}}``."""
    out: dict[str, dict[str, float]] = {}
    for spec in specs or []:
        parts = str(spec).split(":")
        if len(parts) != 3 or parts[0].strip().lower() not in SIDES:
            continue
        try:
            out.setdefault(parts[0].strip().lower(), {})[parts[1].strip()] = float(parts[2])
        except ValueError:
            continue
    return out


def format_grip_offsets(offsets: dict[str, list[float]]) -> list[str]:
    return [f"{side}:{','.join(f'{v:g}' for v in xyz)}"
            for side, xyz in offsets.items() if any(xyz)]


def grip_tuning_file(asset: str | None = None) -> Path:
    """The grip_tuning.json retarget reads for the given CSO hands asset
    (default: the bundled one)."""
    from valve_qc_merger.handswap import asset as assetmod
    source = str(resource_path(Path(asset))) if asset else assetmod.DEFAULT_ASSET
    return Path(tuning.tuning_path(source))


class DeriveDialog(QDialog):
    """Pick the mode, the new asset's name and the options; ``result_spec()``
    then gives ``(mode, name or None, options)``. Modeless in the studio:
    ``apply_requested`` runs without closing (the tuning loop)."""

    apply_requested = Signal()

    def __init__(self, sources: list[str], parent: QWidget | None = None, *,
                 mode: str = "hands", options: dict[str, Any] | None = None,
                 name: str | None = None, existing: set[str] | None = None,
                 kinds: set[str] | None = None) -> None:
        super().__init__(parent)
        self.sources = sources
        self._existing = existing or set()
        self._fixed_name = name  # re-running a derived asset keeps its name
        self.setWindowTitle("Retarget" if name is None else f"Retarget settings — {name}")
        options = dict(options or {})
        # a mode is offered only when every source is a kind it applies to
        allowed = {key for key, spec in DERIVE_MODES.items()
                   if spec.kinds is None or (kinds is not None and kinds <= spec.kinds)}
        if name is None and kinds and kinds <= DERIVE_MODES["grenade"].kinds:
            mode = "grenade"  # zombie hands: making the grenade is the point
        layout = kit.dialog_layout(self)
        what = (f"<b>{sources[0]}</b>" if len(sources) == 1
                else f"<b>{len(sources)} assets</b> ({', '.join(sources[:4])}"
                     f"{'…' if len(sources) > 4 else ''})")
        title = "Retarget" if name is None else f"Retarget settings — {name}"
        layout.addWidget(kit.header(title, f"Make a new asset from {what}; the source "
                                    "stays as it is."))

        # modes as cards; one that does not apply to the sources is not shown
        layout.addWidget(kit.section("Mode"))
        cards = QHBoxLayout()
        cards.setSpacing(8)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self._mode_keys = list(DERIVE_MODES)
        for index, key in enumerate(self._mode_keys):
            icon_name, text = MODE_CARDS[key]
            card = kit.OptionCard(icon_name, DERIVE_MODES[key].title, text)
            self.mode_group.addButton(card, index)
            usable = key == mode if name is not None else key in allowed
            card.setEnabled(usable)
            card.setVisible(usable)
            cards.addWidget(card, 1)
        layout.addLayout(cards)

        name_row = kit.form()
        self.name_edit = QLineEdit()
        self.name_edit.setEnabled(len(sources) == 1 and name is None)
        self.name_edit.setToolTip("name of the asset this makes (re-running keeps it)")
        name_row.addRow("New asset", self.name_edit)
        layout.addLayout(name_row)

        self.pages = QStackedWidget()
        # -- hands -----------------------------------------------------------
        hands_page = QWidget()
        hands_layout = QVBoxLayout(hands_page)
        hands_layout.setContentsMargins(0, 0, 0, 0)
        prefilled = False
        if (name is None and len(sources) == 1 and not options.get("weapon_offset")
                and not options.get("grip_offset") and not options.get("curl")):
            tuned = tuning.load_entry(str(grip_tuning_file(options.get("asset"))), sources[0])
            if tuned:
                options["weapon_offset"] = tuned.get("weapon_offset", [])
                options["grip_offset"] = format_grip_offsets(tuned.get("grip_offset", {}))
                options["curl"] = tuning.curl_specs(tuned.get("curl", {}))
                if "snug_max_deg" in tuned and "snug_max_deg" not in options:
                    options["snug_max_deg"] = tuned["snug_max_deg"]
                prefilled = True
        hands_layout.setSpacing(8)
        hands_layout.addWidget(kit.section("Grip offsets"))
        hands_layout.addWidget(kit.hint("Nudge, Apply, compare with B in the viewport; the "
                                        "fingers re-fit to the weapon after every offset."))
        offsets_form = kit.form()
        self.weapon_offset = Vec3Edit(
            options.get("weapon_offset") or None,
            tooltip="move the weapon relative to both hands (model space, at the grip "
                    "frame); the fingers re-snug afterwards")
        offsets_form.addRow("Weapon (model space)", self.weapon_offset)
        grips = parse_grip_offsets(options.get("grip_offset", []))
        self.grip_offsets: dict[str, Vec3Edit] = {}
        for side in SIDES:
            edit = Vec3Edit(grips.get(side), tooltip=(
                f"shift the {side} palm relative to the weapon, in palm axes: x fingers-"
                "forward, y toward the thumb, z palm normal"))
            self.grip_offsets[side] = edit
            offsets_form.addRow(f"{side.capitalize()} palm (palm axes)", edit)
        tuning_row = QWidget()
        tuning_layout = QHBoxLayout(tuning_row)
        tuning_layout.setContentsMargins(0, 0, 0, 0)
        self.tuning_label = QLabel(f"prefilled from {tuning.FILE_NAME}" if prefilled else "")
        self.tuning_label.setProperty("role", "hint")
        self.save_tuning_button = QPushButton(f"Save to {tuning.FILE_NAME}")
        self.save_tuning_button.setToolTip(
            f"store these offsets as {sources[0]}'s grip tuning: every later retarget of "
            "a weapon folder with this name uses them (CLI and builds too)")
        self.save_tuning_button.setEnabled(len(sources) == 1)
        self.save_tuning_button.clicked.connect(self.save_tuning)
        tuning_layout.addWidget(self.tuning_label, 1)
        tuning_layout.addWidget(self.save_tuning_button)
        offsets_form.addRow(tuning_row)
        hands_layout.addLayout(offsets_form)
        curls = parse_curls(options.get("curl", []))
        self.fingers_section = kit.Collapsible(
            "Fingers — extra curl per joint", self._fingers_box(curls),
            open_=any(v for side in curls.values() for v in side.values()))
        hands_layout.addWidget(self.fingers_section)
        hands_layout.addWidget(kit.section("Options"))
        self.retarget_form = OptionsForm(
            RetargetOptions, "retarget",
            {k: v for k, v in options.items() if k not in _OFFSET_FIELDS},
            exclude=_OFFSET_FIELDS | _RETARGET_FIXED)
        hands_layout.addWidget(self.retarget_form)
        hands_layout.addStretch(1)
        self.pages.addWidget(hands_page)
        # -- canon -----------------------------------------------------------
        self.canon_form = OptionsForm(CanonicalizeOptions, "canonicalize",
                                      options if mode == "canon" else {},
                                      exclude=frozenset({"model_dir"}))
        canon_page = QWidget()
        canon_layout = QVBoxLayout(canon_page)
        canon_layout.setContentsMargins(0, 0, 0, 0)
        canon_layout.addWidget(self.canon_form)
        canon_layout.addStretch(1)
        self.pages.addWidget(canon_page)
        # -- grenade ---------------------------------------------------------
        self.grenade_form = OptionsForm(ZhandsGrenadeOptions, "zhands-grenade",
                                        options if mode == "grenade" else {},
                                        exclude=frozenset({"knife_dir", "out", "modelname"}))
        grenade_page = QWidget()
        grenade_layout = QVBoxLayout(grenade_page)
        grenade_layout.setContentsMargins(0, 0, 0, 0)
        grenade_layout.addWidget(self.grenade_form)
        grenade_layout.addStretch(1)
        self.pages.addWidget(grenade_page)
        # the options scroll on a short screen instead of being squeezed
        scroll = QScrollArea()
        scroll.setWidget(self.pages)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Apply
                                   | QDialogButtonBox.StandardButton.Cancel)
        self.run_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.run_button.setText("Run")
        self.run_button.setDefault(True)  # Enter runs; the mode cards never take it
        self.run_button.setToolTip("run and close")
        self.apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setToolTip("run and keep this dialog open: nudge the offsets, "
                                     "Apply again, compare in the viewport (B = before)")
        self.apply_button.clicked.connect(self._apply)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.mode_group.idToggled.connect(self._mode_toggled)
        self.mode_group.button(self._mode_keys.index(mode)).setChecked(True)
        self._mode_toggled(self._mode_keys.index(mode), True)
        # wide enough for the widest options page (the scroll area never
        # scrolls sideways): its minimum + the vertical scroll bar + margins
        width = max(580, max(self.pages.widget(i).minimumSizeHint().width()
                             for i in range(self.pages.count())) + 48)
        self.resize(width, min(860, self.screen().availableGeometry().height() - 60)
                    if self.screen() is not None else 760)

    @property
    def mode(self) -> str:
        return self._mode_keys[max(self.mode_group.checkedId(), 0)]

    def _default_name(self, mode: str) -> str:
        return DERIVE_MODES[mode].default_name(self.sources[0])

    def _mode_toggled(self, index: int, checked: bool) -> None:
        if not checked:
            return
        mode = self._mode_keys[index]
        self.pages.setCurrentIndex(index)
        if self._fixed_name is not None:
            self.name_edit.setText(self._fixed_name)
        elif len(self.sources) == 1:
            current = self.name_edit.text()
            if not current or current in {self._default_name(m) for m in DERIVE_MODES}:
                self.name_edit.setText(self._default_name(mode))
        else:
            self.name_edit.setText(DERIVE_MODES[mode].default_name("<source>"))

    def options(self) -> dict[str, Any]:
        """The chosen options (only non-defaults); raises ValueError on bad input."""
        if self.mode == "canon":
            return self.canon_form.values()
        if self.mode == "grenade":
            return self.grenade_form.values()
        values = self.retarget_form.values()
        weapon = self.weapon_offset.value()
        if any(weapon):
            values["weapon_offset"] = weapon
        grips = format_grip_offsets({s: e.value() for s, e in self.grip_offsets.items()})
        if grips:
            values["grip_offset"] = grips
        curls = tuning.curl_specs(self.curls())
        if curls:
            values["curl"] = curls
        return values

    def _fingers_box(self, curls: dict[str, dict[str, float]]) -> QWidget:
        box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        grid = QGridLayout()
        outer.addLayout(grid)
        for column, title in enumerate(FINGER_TITLES.values(), start=1):
            grid.addWidget(QLabel(title), 0, column)
        self.curl_spins: dict[tuple[str, str], QSpinBox] = {}
        for row, side in enumerate(SIDES, start=1):
            grid.addWidget(QLabel(side.capitalize()), row, 0)
            for column, finger in enumerate(FINGER_TITLES, start=1):
                spin = QSpinBox()
                spin.setRange(-45, 45)
                spin.setSingleStep(2)
                spin.setValue(int(round(curls.get(side, {}).get(finger, 0))))
                spin.setMinimumHeight(spin.sizeHint().height())
                spin.setToolTip(f"{side} {FINGER_TITLES[finger].lower()}: added to every "
                                "joint after the automatic fit (it re-snugs too)")
                grid.addWidget(spin, row, column)
                self.curl_spins[(side, finger)] = spin
        outer.addWidget(kit.hint(
            "Degrees added to every joint after the automatic fit: + closes, − opens. A "
            "loose grip (our hands are bigger than most originals): close a few degrees, "
            "or raise 'Max finger fit' below so the automatic fit may curl further."))
        return box

    def curls(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for (side, finger), spin in self.curl_spins.items():
            if spin.value():
                out.setdefault(side, {})[finger] = float(spin.value())
        return out

    def tuning_file(self) -> Path:
        return grip_tuning_file(self.retarget_form.values().get("asset"))

    def save_tuning(self) -> dict | None:
        """Write the current offsets as the source weapon's grip tuning."""
        if len(self.sources) != 1:
            return None
        path = self.tuning_file()
        try:
            entry = tuning.save_entry(
                str(path), self.sources[0],
                grip_offset={s: e.value() for s, e in self.grip_offsets.items()},
                weapon_offset=self.weapon_offset.value(), curl=self.curls(),
                snug_max_deg=self.retarget_form.values().get("snug_max_deg"))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Grip tuning", f"Cannot write {path}: {exc}")
            return None
        self.tuning_label.setText(f"saved for {self.sources[0]}" if entry
                                  else f"{self.sources[0]} removed from {tuning.FILE_NAME}")
        return entry

    def target_name(self) -> str | None:
        """The new asset's name (None: the default per source, batch mode)."""
        if self._fixed_name is not None:
            return self._fixed_name
        if len(self.sources) != 1:
            return None
        return self.name_edit.text().strip() or self._default_name(self.mode)

    def set_busy(self, busy: bool) -> None:
        """A job is running: Run/Apply wait for it."""
        self.run_button.setEnabled(not busy)
        self.apply_button.setEnabled(not busy)

    def _apply(self) -> None:
        if not self._validate():
            return
        self.apply_requested.emit()
        # later Applies re-run the asset(s) just made: no more replace prompts,
        # and the mode/name are now those of the result
        if self._fixed_name is None:
            if len(self.sources) == 1:
                self._fixed_name = self.target_name()
                self.name_edit.setEnabled(False)
            self._existing = set()
            for index, key in enumerate(self._mode_keys):
                self.mode_group.button(index).setEnabled(key == self.mode)

    def _accept(self) -> None:
        if self._validate():
            self.accept()

    def _validate(self) -> bool:
        try:
            self.options()
        except ValueError as exc:
            QMessageBox.warning(self, "Retarget", f"Invalid option: {exc}")
            return False
        name = self.target_name()
        if name is not None and name in self.sources:
            QMessageBox.warning(self, "Retarget", "The new asset needs its own name.")
            return False
        if self._fixed_name is None:
            names = [name] if name is not None else [
                DERIVE_MODES[self.mode].default_name(s) for s in self.sources]
            taken = [n for n in names if n in self._existing]
            if taken:
                answer = QMessageBox.question(
                    self, "Retarget", f"Replace existing asset(s): {', '.join(taken[:6])}?")
                if answer != QMessageBox.StandardButton.Yes:
                    return False
        return True

    def result_spec(self) -> tuple[str, str | None, dict[str, Any]]:
        return self.mode, self.target_name(), self.options()


__all__ = ["DeriveDialog", "Vec3Edit", "format_grip_offsets", "grip_tuning_file",
           "parse_grip_offsets"]
