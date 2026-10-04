"""Sprites in the studio: the sprite / HUD panel and the two make dialogs."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.sprite.hud import parse_txt
from valve_qc_merger.sprite.spr import FORMATS, TYPES, SprError, Sprite, read_spr
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import icon

FORMAT_HINTS = {
    "normal": "opaque colours",
    "additive": "added to the scene — glows, muzzle flashes, HUD icons; black is invisible",
    "indexalpha": "one colour, the image's transparency kept — smoke, decals",
    "alphatest": "cut-out: transparent pixels become holes",
}


def load_rgba(path: Path) -> np.ndarray:
    """An image file (PNG, BMP, TGA, JPG...) as (h, w, 4) uint8 RGBA."""
    image = QImage(str(path))
    if image.isNull():
        raise ValueError(f"cannot read {Path(path).name} as an image")
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    data = np.frombuffer(image.constBits(), np.uint8, image.sizeInBytes())
    return data.reshape(image.height(), image.bytesPerLine())[:, :image.width() * 4] \
        .reshape(image.height(), image.width(), 4).copy()


def rgba_pixmap(rgba: np.ndarray, scale: int = 1) -> QPixmap:
    h, w = rgba.shape[:2]
    image = QImage(np.ascontiguousarray(rgba).tobytes(), w, h, w * 4,
                   QImage.Format.Format_RGBA8888).copy()
    if scale > 1:
        image = image.scaled(w * scale, h * scale, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.FastTransformation)
    return QPixmap.fromImage(image)


class SpritePanel(QWidget):
    """A sprite (animated preview, type, format) or a HUD txt (its entries
    and the icons they cut from the library sheets)."""

    remove_requested = Signal(str)
    reveal_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = ""
        self.sprite: Sprite | None = None
        self._frame = 0
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._next_frame)
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
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.title)
        self.where = kit.hint("")
        layout.addWidget(self.where)
        actions = QHBoxLayout()
        reveal = QPushButton(icon("folder-search"), "Folder")
        reveal.clicked.connect(lambda: self.name and self.reveal_requested.emit(self.name))
        remove = QPushButton(icon("trash-2"), "Remove")
        remove.clicked.connect(lambda: self.name and self.remove_requested.emit(self.name))
        actions.addWidget(reveal)
        actions.addWidget(remove)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(140)
        self.preview.setStyleSheet(
            f"background: {theme.TOKENS['bg']}; border: 1px solid {theme.TOKENS['border']};"
            " border-radius: 8px;")
        layout.addWidget(self.preview)
        frames = QHBoxLayout()
        self.play_button = QToolButton()
        self.play_button.setIcon(icon("play"))
        self.play_button.setCheckable(True)
        self.play_button.toggled.connect(self._toggle_play)
        self.frame_slider = QSlider(Qt.Orientation.Horizontal)
        self.frame_slider.valueChanged.connect(self._show_frame)
        self.frame_label = QLabel()
        frames.addWidget(self.play_button)
        frames.addWidget(self.frame_slider, 1)
        frames.addWidget(self.frame_label)
        self.frames_row = QWidget()
        self.frames_row.setLayout(frames)
        layout.addWidget(self.frames_row)
        tiles = QGridLayout()
        tiles.setSpacing(6)
        from valve_qc_merger.studio.widgets import StatTile
        self.values: dict[str, QLabel] = {}
        for index, caption in enumerate(("type", "format", "size", "frames")):
            tile = StatTile(caption)
            self.values[caption] = tile.value
            tiles.addWidget(tile, 0, index)
        self.tiles = QWidget()
        self.tiles.setLayout(tiles)
        layout.addWidget(self.tiles)
        self.format_hint = kit.hint("")
        layout.addWidget(self.format_hint)
        self.entries = QTableWidget(0, 5)
        self.entries.setHorizontalHeaderLabels(["Entry", "Res", "Sheet", "Rect", "Icon"])
        self.entries.verticalHeader().setVisible(False)
        self.entries.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch)
        self.entries.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.entries.setMinimumHeight(320)  # a weapon HUD has 8-10 entries
        layout.addWidget(self.entries)
        layout.addStretch(1)

    def show_sprite(self, name: str, path: Path, library: Path) -> None:
        self.timer.stop()
        self.play_button.setChecked(False)
        self.name = name
        self.title.setText(Path(name).name)
        self.where.setText(f"sprites/{name}")
        is_txt = name.lower().endswith(".txt")
        self.entries.setVisible(is_txt)
        for widget in (self.frames_row, self.tiles, self.format_hint):
            widget.setVisible(not is_txt)
        if is_txt:
            self._show_txt(path, library)
            return
        try:
            self.sprite = read_spr(path)
        except (SprError, OSError) as exc:
            self.sprite = None
            self.preview.setText(f"cannot read: {exc}")
            return
        sprite = self.sprite
        width, height = sprite.size
        self.values["type"].setText(sprite.type_name.replace("_", " "))
        self.values["format"].setText(sprite.format_name)
        self.values["size"].setText(f"{width}×{height}")
        self.values["frames"].setText(str(len(sprite.frames)))
        self.format_hint.setText(FORMAT_HINTS.get(sprite.format_name, ""))
        self.frame_slider.setRange(0, max(len(sprite.frames) - 1, 0))
        self.frames_row.setVisible(len(sprite.frames) > 1)
        self.frame_slider.setValue(0)
        self._show_frame(0)

    def _scale(self, w: int, h: int) -> int:
        return max(1, min(4, 320 // max(w, 1), 200 // max(h, 1)))

    def _show_frame(self, index: int) -> None:
        if self.sprite is None or not self.sprite.frames:
            return
        index = min(index, len(self.sprite.frames) - 1)
        self._frame = index
        rgba = self.sprite.rgba(index)
        self.preview.setPixmap(rgba_pixmap(rgba, self._scale(rgba.shape[1], rgba.shape[0])))
        self.frame_label.setText(f"{index + 1} / {len(self.sprite.frames)}")

    def _next_frame(self) -> None:
        if self.sprite is not None and self.sprite.frames:
            self.frame_slider.setValue((self._frame + 1) % len(self.sprite.frames))

    def _toggle_play(self, on: bool) -> None:
        self.play_button.setIcon(icon("pause" if on else "play"))
        if on:
            self.timer.start()
        else:
            self.timer.stop()

    def _show_txt(self, path: Path, library: Path) -> None:
        self.sprite = None
        try:
            entries = parse_txt(path.read_text(encoding="latin-1", errors="replace"))
        except OSError as exc:
            self.preview.setText(f"cannot read: {exc}")
            return
        sheets: dict[str, Sprite | None] = {}
        self.entries.setRowCount(len(entries))
        first: QPixmap | None = None
        for row, (entry, res, sheet, x, y, w, h) in enumerate(entries):
            for col, text in enumerate((entry, res, sheet, f"{x},{y} {w}×{h}")):
                self.entries.setItem(row, col, QTableWidgetItem(str(text)))
            if sheet not in sheets:
                try:
                    sheets[sheet] = read_spr(library / f"{sheet}.spr")
                except (SprError, OSError):
                    sheets[sheet] = None
            art = sheets[sheet]
            label = QLabel()
            if art is not None and art.frames:
                crop = art.rgba(0)[y:y + h, x:x + w]
                if crop.size:
                    pixmap = rgba_pixmap(crop)
                    label.setPixmap(pixmap)
                    first = first or rgba_pixmap(crop, 2)
            else:
                label.setText("stock sheet" if sheet in ("crosshairs", "autoaim")
                              or sheet.startswith(("320hud", "640hud")) else "sheet missing")
                theme.set_role(label, "faint")
            self.entries.setCellWidget(row, 4, label)
        self.entries.resizeRowsToContents()
        if first is not None:
            self.preview.setPixmap(first)
        else:
            self.preview.setText("no icon from this project's sheets")


def _image_row(edit: QLineEdit, title: str) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(edit, 1)
    browse = QToolButton()
    browse.setIcon(icon("folder-open"))
    browse.clicked.connect(lambda: _pick_image(edit, title))
    layout.addWidget(browse)
    return row


def _pick_image(edit: QLineEdit, title: str) -> None:
    path, _ = QFileDialog.getOpenFileName(edit, title, edit.text(),
                                          "Images (*.png *.bmp *.tga *.jpg *.jpeg)")
    if path:
        edit.setText(path)


class NewSpriteDialog(QDialog):
    """Images (one per frame) -> sprites/<name>.spr."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New sprite")
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header("New sprite", "Images become the frames of one "
                                    "GoldSource sprite (one shared 256-colour palette)."))
        self.frames = QListWidget()
        self.frames.setMinimumHeight(110)
        add = QPushButton(icon("image-plus"), "Add images…")
        add.setAutoDefault(False)
        add.clicked.connect(self._add)
        clear = QPushButton("Clear")
        clear.setAutoDefault(False)
        clear.clicked.connect(self.frames.clear)
        buttons_row = QHBoxLayout()
        buttons_row.addWidget(add)
        buttons_row.addWidget(clear)
        buttons_row.addStretch(1)
        layout.addWidget(kit.section("Frames"))
        layout.addWidget(self.frames)
        layout.addLayout(buttons_row)
        form = kit.form()
        self.name_edit = QLineEdit("effects/my_sprite")
        self.name_edit.setToolTip("Path under sprites/ (no .spr)")
        self.format_box = QComboBox()
        for fmt in FORMATS:
            self.format_box.addItem(fmt, fmt)
        self.format_box.setCurrentText("additive")
        self.type_box = QComboBox()
        for stype in TYPES:
            self.type_box.addItem(stype.replace("_", " "), stype)
        self.type_box.setCurrentText("parallel")
        self.hint = kit.hint("")
        self.format_box.currentTextChanged.connect(
            lambda t: self.hint.setText(FORMAT_HINTS.get(t, "")))
        self.hint.setText(FORMAT_HINTS["additive"])
        form.addRow("Name", self.name_edit)
        form.addRow("Format", self.format_box)
        form.addRow("", self.hint)
        form.addRow("Faces", self.type_box)
        layout.addLayout(form)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self.resize(540, self.sizeHint().height())

    def _add(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Frames", "",
                                                "Images (*.png *.bmp *.tga *.jpg *.jpeg)")
        for path in files:
            self.frames.addItem(path)

    def frame_paths(self) -> list[Path]:
        return [Path(self.frames.item(i).text()) for i in range(self.frames.count())]


class WeaponHudDialog(QDialog):
    """Icons -> sprites/weapon_<name>.txt + its sheet sprite."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New weapon HUD")
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header(
            "New weapon HUD", "The weapon-list icon (fitted to 170×45, additive: black is "
            "invisible), an optional selected icon and ammo icon, packed into one sheet."))
        form = kit.form()
        self.weapon_edit = QLineEdit("weapon_")
        self.weapon_edit.setToolTip("The weapon's HUD name — sprites/<name>.txt "
                                    "(what your plugin's WeaponList sends)")
        self.icon_edit = QLineEdit()
        self.selected_edit = QLineEdit()
        self.selected_edit.setPlaceholderText("optional: same as the icon")
        self.ammo_edit = QLineEdit()
        self.ammo_edit.setPlaceholderText("optional: 24×24")
        form.addRow("Weapon", self.weapon_edit)
        form.addRow("Icon", _image_row(self.icon_edit, "Weapon icon"))
        form.addRow("Selected icon", _image_row(self.selected_edit, "Selected icon"))
        form.addRow("Ammo icon", _image_row(self.ammo_edit, "Ammo icon"))
        layout.addLayout(form)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self.resize(560, self.sizeHint().height())


__all__ = ["NewSpriteDialog", "SpritePanel", "WeaponHudDialog", "load_rgba", "rgba_pixmap"]
