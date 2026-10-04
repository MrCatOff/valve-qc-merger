"""WAD3 in the studio: make a spray (``tempdecal.wad``), browse any WAD."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QWidget,
)

from valve_qc_merger.sprite.wad import (
    Texture,
    WadError,
    make_spray,
    read_wad,
    spray_size,
    write_wad,
)
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import icon
from valve_qc_merger.studio.sprite_panel import _image_row, load_rgba, rgba_pixmap


def checkered(rgba: np.ndarray, cell: int = 8) -> np.ndarray:
    """``rgba`` over a grey checkerboard (what is transparent shows)."""
    h, w = rgba.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    board = np.where(((yy // cell + xx // cell) % 2)[..., None] == 0, 90, 60)
    alpha = rgba[..., 3:4] / 255.0
    out = np.empty_like(rgba)
    out[..., :3] = (rgba[..., :3] * alpha + board * (1 - alpha)).astype(np.uint8)
    out[..., 3] = 255
    return out


class SprayDialog(QDialog):
    """An image -> ``tempdecal.wad`` (the spray a player's client sends)."""

    def __init__(self, folder: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New spray")
        self.folder = folder
        self.image: np.ndarray | None = None
        self.texture: Texture | None = None
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header(
            "New spray", "Turn an image into tempdecal.wad: the {LOGO texture, 255 colours, "
            "transparent where the image is. Put it in cstrike/ (read-only, or the game "
            "overwrites it with the spray chosen in Options)."))
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("PNG, BMP, TGA or JPG")
        self.path_edit.textChanged.connect(lambda _t: self._load())
        self.size_box = QComboBox()
        self.size_box.currentIndexChanged.connect(lambda _i: self._update())
        form = kit.form()
        form.addRow("Image", _image_row(self.path_edit, "Spray image"))
        form.addRow("Size", self.size_box)
        layout.addLayout(form)
        layout.addWidget(kit.hint(
            "Sides are multiples of 16, at most 256, and 14336 pixels in all (112 × 128). "
            "Stock HLDS only takes 64 × 64; ReHLDS takes bigger with "
            "sv_rehlds_allow_large_sprays 1 (its default)."))
        self.preview = QLabel("No image yet.")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(280)
        theme.set_role(self.preview, "muted")
        layout.addWidget(self.preview, 1)
        row = QHBoxLayout()
        self.status = QLabel()
        theme.set_role(self.status, "muted")
        self.save_button = QPushButton(icon("save", theme.TOKENS["on_accent"]),
                                       "Save tempdecal.wad…")
        theme.set_primary(self.save_button)
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(lambda: self.save())
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        for button in (self.save_button, close):
            button.setAutoDefault(False)
        row.addWidget(self.status, 1)
        row.addWidget(close)
        row.addWidget(self.save_button)
        layout.addLayout(row)
        self.resize(560, 560)

    def _load(self) -> None:
        path = Path(self.path_edit.text().strip())
        self.size_box.blockSignals(True)
        self.size_box.clear()
        self.image = None
        if path.is_file():
            try:
                self.image = load_rgba(path)
            except ValueError as exc:
                self.status.setText(str(exc))
        if self.image is not None:
            h, w = self.image.shape[:2]
            sizes = [spray_size(w, h), spray_size(w, h, max_area=4096, max_side=64)]
            labels = ["largest", "any server (≤ 64 × 64)"]
            if spray_size(w, h, upscale=True) != sizes[0]:
                sizes.insert(1, spray_size(w, h, upscale=True))
                labels.insert(1, "largest, scaled up")
            for size, label in zip(sizes, labels, strict=True):
                if self.size_box.findData(size) < 0:
                    self.size_box.addItem(f"{size[0]} × {size[1]}  —  {label}", size)
        self.size_box.blockSignals(False)
        self._update()

    def _update(self) -> None:
        self.texture = None
        if self.image is not None and self.size_box.currentData():
            self.texture = make_spray(self.image, self.size_box.currentData())
            rgba = self.texture.rgba()
            scale = max(1, min(4, 256 // max(rgba.shape[:2])))
            self.preview.setPixmap(rgba_pixmap(checkered(rgba), scale))
            clear = int((rgba[..., 3] == 0).sum())
            self.status.setText(f"{self.texture.width} × {self.texture.height}, "
                                f"{clear} transparent pixel(s)")
        elif self.image is None:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("No image yet.")
        self.save_button.setEnabled(self.texture is not None)

    def save(self, path: Path | None = None) -> Path | None:
        if self.texture is None:
            return None
        if path is None:
            start = str(Path(self.folder) / "tempdecal.wad") if self.folder else "tempdecal.wad"
            chosen, _ = QFileDialog.getSaveFileName(self, "Save spray", start, "WAD (*.wad)")
            if not chosen:
                return None
            path = Path(chosen)
        path.write_bytes(write_wad([self.texture]))
        self.status.setText(f"Saved {path}")
        return path


class WadViewer(QDialog):
    """Every texture of a WAD3 as a thumbnail; export one as PNG."""

    def __init__(self, path: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("WAD viewer")
        self.textures: list[Texture] = []
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header(
            "WAD viewer", "Map textures, decals.wad, sprays: every texture of a WAD3. "
            "Maps name the WADs they need in their worldspawn — a server must send them."))
        row = QHBoxLayout()
        self.path_edit = QLineEdit(path)
        self.path_edit.setPlaceholderText("a .wad file")
        self.path_edit.editingFinished.connect(self.load)
        pick = QPushButton(icon("folder-open"), "")
        pick.clicked.connect(self._pick)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(lambda _t: self._apply_filter())
        pick.setAutoDefault(False)
        row.addWidget(self.path_edit, 1)
        row.addWidget(pick)
        row.addWidget(self.filter_edit)
        layout.addLayout(row)
        self.grid = QListWidget()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setIconSize(QSize(96, 96))
        self.grid.setGridSize(QSize(120, 130))
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setWordWrap(True)
        self.grid.currentRowChanged.connect(lambda _r: self._show_current())
        layout.addWidget(self.grid, 1)
        bottom = QHBoxLayout()
        self.info = QLabel()
        theme.set_role(self.info, "muted")
        self.export_button = QPushButton(icon("file-down"), "Export PNG…")
        self.export_button.setAutoDefault(False)
        self.export_button.clicked.connect(lambda: self.export_current())
        bottom.addWidget(self.info, 1)
        bottom.addWidget(self.export_button)
        layout.addLayout(bottom)
        self.resize(760, 600)
        if path:
            self.load()

    def _pick(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open WAD", self.path_edit.text(),
                                              "WAD3 (*.wad)")
        if path:
            self.path_edit.setText(path)
            self.load()

    def load(self) -> None:
        self.grid.clear()
        self.textures = []
        path = Path(self.path_edit.text().strip())
        if not path.is_file():
            self.info.setText("Choose a .wad file.")
            return
        try:
            self.textures = read_wad(path)
        except (WadError, OSError) as exc:
            self.info.setText(str(exc))
            return
        for index, texture in enumerate(self.textures):
            rgba = checkered(texture.rgba(), 4)
            pixmap = rgba_pixmap(rgba).scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.FastTransformation)
            item = QListWidgetItem(QIcon(pixmap), texture.name)
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(f"{texture.name}  {texture.width} × {texture.height}")
            self.grid.addItem(item)
        self.info.setText(f"{len(self.textures)} texture(s) in {path.name}")
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self.filter_edit.text().strip().lower()
        for row in range(self.grid.count()):
            item = self.grid.item(row)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def current(self) -> Texture | None:
        item = self.grid.currentItem()
        return None if item is None else self.textures[item.data(Qt.ItemDataRole.UserRole)]

    def _show_current(self) -> None:
        texture = self.current()
        if texture is not None:
            kind = ("decal (index = opacity)" if texture.alpha_decal else
                    "transparent (index 255)" if texture.transparent else "opaque")
            self.info.setText(f"{texture.name}: {texture.width} × {texture.height}, {kind}")

    def export_current(self, path: Path | None = None) -> Path | None:
        from valve_qc_merger.preview.render import encode_png
        texture = self.current()
        if texture is None:
            return None
        if path is None:
            chosen, _ = QFileDialog.getSaveFileName(self, "Export texture",
                                                    f"{texture.name.strip('{')}.png",
                                                    "PNG (*.png)")
            if not chosen:
                return None
            path = Path(chosen)
        path.write_bytes(encode_png(texture.rgba()))
        self.info.setText(f"Saved {path}")
        return path


__all__ = ["SprayDialog", "WadViewer", "checkered"]
