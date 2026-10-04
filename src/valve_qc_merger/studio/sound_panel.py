"""The right-hand panel for a sound of the project's library, and Fix sound."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.sound.wav import ENGINE_RATES, FixOptions, Wav, WavError, problems, read_wav
from valve_qc_merger.studio import audio, theme
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio.icons import icon

_STATUS_CACHE: dict[tuple[str, float], list[str]] = {}


def sound_problems(path: Path) -> list[str]:
    """Engine problems of a WAV, cached by file time (Explorer badges)."""
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return ["missing"]
    if key not in _STATUS_CACHE:
        try:
            _STATUS_CACHE[key] = problems(read_wav(path))
        except (WavError, OSError, ValueError) as exc:
            _STATUS_CACHE[key] = [f"cannot be read: {exc}"]
    return _STATUS_CACHE[key]


class WaveformView(QWidget):
    """Min/max of the mono mix per pixel column, loop cue points marked."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.wav: Wav | None = None
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_wav(self, wav: Wav | None) -> None:
        self.wav = wav
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: ANN001, N802 - Qt override
        from valve_qc_merger.sound.wav import waveform
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(theme.color("border"), 1))
        painter.setBrush(theme.color("surface"))
        painter.drawRoundedRect(rect, 8, 8)
        mid = rect.center().y()
        painter.setPen(QPen(theme.color("border_strong"), 1))
        painter.drawLine(QPointF(rect.left() + 8, mid), QPointF(rect.right() - 8, mid))
        if self.wav is None or self.wav.samples is None or not self.wav.frames:
            painter.setPen(theme.color("faint"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "no waveform" if self.wav is not None else "")
            return
        inner = rect.adjusted(8, 8, -8, -8)
        columns = max(int(inner.width()), 1)
        peaks = waveform(self.wav, columns)
        half = inner.height() / 2
        painter.setPen(QPen(theme.color("accent"), 1))
        for x, (low, high) in enumerate(peaks):
            px = inner.left() + x
            painter.drawLine(QPointF(px, mid - high * half), QPointF(px, mid - low * half))
        painter.setPen(QPen(theme.color("warning"), 1.5, Qt.PenStyle.DashLine))
        for cue in self.wav.cues:
            px = inner.left() + inner.width() * min(cue / self.wav.frames, 1.0)
            painter.drawLine(QPointF(px, inner.top()), QPointF(px, inner.bottom()))


class FixDialog(QDialog):
    """How to make sounds engine-safe: mono, rate, loudness, silence."""

    def __init__(self, names: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Fix sounds")
        layout = kit.dialog_layout(self)
        what = names[0] if len(names) == 1 else f"{len(names)} sounds"
        layout.addWidget(kit.header("Fix sounds", f"Rewrite {what} as 16-bit PCM the engine "
                                    "plays everywhere. The original is kept: Undo fix "
                                    "restores it."))
        self.mono = QCheckBox("Mix to mono (world sounds must be mono)")
        self.mono.setChecked(True)
        self.rate = QComboBox()
        self.rate.addItem("Keep a valid rate, else the nearest higher one", None)
        for rate in ENGINE_RATES:
            self.rate.addItem(f"{rate} Hz", rate)
        self.normalize = QCheckBox("Normalise the peak to")
        self.level = QDoubleSpinBox()
        self.level.setLocale(kit.NUMBER_LOCALE)
        self.level.setRange(-24.0, 0.0)
        self.level.setValue(-1.0)
        self.level.setSuffix(" dBFS")
        self.level.setEnabled(False)
        self.normalize.toggled.connect(self.level.setEnabled)
        self.trim = QCheckBox("Trim silence at both ends (never for looping sounds)")
        form = kit.form()
        form.addRow("", self.mono)
        form.addRow("Sample rate", self.rate)
        row = QHBoxLayout()
        row.addWidget(self.normalize)
        row.addWidget(self.level)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        form.addRow("", holder)
        form.addRow("", self.trim)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Fix")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(520, self.sizeHint().height())

    def options(self) -> FixOptions:
        return FixOptions(mono=self.mono.isChecked(), rate=self.rate.currentData(),
                          normalize_db=self.level.value() if self.normalize.isChecked()
                          else None, trim=self.trim.isChecked())


def _tile(caption: str) -> tuple[QWidget, QLabel]:
    from valve_qc_merger.studio.widgets import StatTile
    tile = StatTile(caption)
    return tile, tile.value


class SoundPanel(QWidget):
    """A sound: waveform, format, what the engine dislikes, who plays it."""

    fix_requested = Signal(list)  # sound names
    undo_requested = Signal(str)
    remove_requested = Signal(str)
    reveal_requested = Signal(str)
    asset_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = ""
        self.path: Path | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        page = QWidget()
        scroll.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        self.title = QLabel("—")
        theme.set_role(self.title, "heading")
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.title)
        self.where = kit.hint("")
        self.where.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.where)
        actions = QHBoxLayout()
        self.play_button = QPushButton(icon("play"), "Play")
        self.play_button.clicked.connect(self.play)
        stop = QPushButton(icon("square"), "Stop")
        stop.clicked.connect(audio.stop)
        self.fix_button = QPushButton(icon("wand-sparkles"), "Fix…")
        self.fix_button.clicked.connect(lambda: self.name and self.fix_requested.emit([self.name]))
        self.undo_button = QPushButton(icon("refresh-cw"), "Undo fix")
        self.undo_button.clicked.connect(lambda: self.name and self.undo_requested.emit(self.name))
        reveal = QPushButton(icon("folder-search"), "Folder")
        reveal.clicked.connect(lambda: self.name and self.reveal_requested.emit(self.name))
        remove = QPushButton(icon("trash-2"), "Remove")
        remove.clicked.connect(lambda: self.name and self.remove_requested.emit(self.name))
        for button in (self.play_button, stop, self.fix_button, self.undo_button, reveal,
                       remove):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.play_button.setEnabled(audio.available())
        if not audio.available():
            self.play_button.setToolTip("No audio player found on this system")

        self.wave = WaveformView()
        layout.addWidget(self.wave)
        tiles = QGridLayout()
        tiles.setSpacing(6)
        self.values: dict[str, QLabel] = {}
        for index, caption in enumerate(("format", "channels", "rate", "bits", "length",
                                         "peak", "loop", "size")):
            tile, value = _tile(caption)
            self.values[caption] = value
            tiles.addWidget(tile, index // 4, index % 4)
        layout.addLayout(tiles)
        layout.addWidget(kit.section("Engine check"))
        self.verdict = QLabel()
        self.verdict.setWordWrap(True)
        layout.addWidget(self.verdict)
        layout.addWidget(kit.section("Played by"))
        self.users_box = QVBoxLayout()
        self.users_box.setSpacing(2)
        layout.addLayout(self.users_box)
        layout.addStretch(1)

    def show_sound(self, name: str, path: Path | None, users: list[str],
                   can_undo: bool) -> None:
        self.name, self.path = name, path
        self.title.setText(Path(name).name if name else "—")
        self.where.setText(f"sound/{name}" if name else "")
        self.undo_button.setVisible(can_undo)
        wav = None
        error = ""
        if path is not None:
            try:
                wav = read_wav(path)
            except (WavError, OSError, ValueError) as exc:
                error = str(exc)
        self.wave.set_wav(wav)
        if wav is None:
            for value in self.values.values():
                value.setText("—")
            self._verdict([f"cannot be read: {error or 'missing file'}"])
        else:
            peak = 20 * math.log10(wav.peak) if wav.peak > 0 else -math.inf
            loop = (f"at {wav.cues[0] / wav.rate:.2f} s" if wav.cues and wav.rate else "no")
            values = {"format": wav.format_name, "channels": "mono" if wav.channels == 1
                      else f"{wav.channels} (stereo)" if wav.channels == 2 else wav.channels,
                      "rate": f"{wav.rate} Hz", "bits": f"{wav.bits}-bit",
                      "length": f"{wav.duration:.2f} s",
                      "peak": f"{peak:.1f} dB" if np.isfinite(peak) else "silent",
                      "loop": loop, "size": f"{wav.size / 1024:.0f} KB"}
            for key, value in values.items():
                self.values[key].setText(str(value))
            self._verdict(problems(wav))
        while self.users_box.count():
            widget = self.users_box.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        if not users:
            self.users_box.addWidget(kit.hint("No model of the project plays it."))
        for asset in users:
            link = QPushButton(asset)
            theme.set_role(link, "link")
            link.setCursor(Qt.CursorShape.PointingHandCursor)
            link.clicked.connect(lambda _=False, a=asset: self.asset_requested.emit(a))
            self.users_box.addWidget(link, 0, Qt.AlignmentFlag.AlignLeft)

    def _verdict(self, found: list[str]) -> None:
        if not found:
            self.verdict.setText("✓ Engine-ready: 8/16-bit PCM, mono, an engine rate.")
            theme.set_role(self.verdict, "success")
        else:
            self.verdict.setText("\n".join(f"▲ {p}" for p in found))
            theme.set_role(self.verdict, "warning")

    def play(self) -> None:
        if self.path is not None:
            audio.play(self.path)


__all__ = ["FixDialog", "SoundPanel", "WaveformView", "sound_problems"]
