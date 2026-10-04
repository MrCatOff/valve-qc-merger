"""WAD3 textures, spray logos (tempdecal.wad) and the studio tools."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from valve_qc_merger.sprite.wad import (
    MIPTEX,
    SPRAY,
    SPRAY_NAME,
    Texture,
    WadError,
    make_spray,
    read_wad,
    spray_size,
    write_wad,
)


def _logo(size: int = 300) -> np.ndarray:
    image = np.zeros((size, size, 4), np.uint8)
    image[size // 6:-size // 6, size // 6:-size // 6] = (200, 30, 30, 255)
    image[size // 3:-size // 3, size // 3:-size // 3] = (20, 220, 40, 255)
    return image


def test_spray_sizes() -> None:
    assert spray_size(64, 64) == (64, 64)  # never scaled up by default
    assert spray_size(64, 64, upscale=True) == (112, 112)
    assert spray_size(1920, 1080) == (144, 80)
    for w, h in [(1920, 1080), (300, 300), (1000, 120), (37, 999)]:
        sw, sh = spray_size(w, h)
        assert sw % 16 == 0 and sh % 16 == 0 and sw * sh <= 14336 and max(sw, sh) <= 256
    assert spray_size(500, 500, max_area=4096, max_side=64) == (64, 64)


def test_make_spray_round_trip() -> None:
    texture = make_spray(_logo())
    assert (texture.name, texture.lump_type) == (SPRAY_NAME, SPRAY)
    assert (texture.width, texture.height) == (112, 112) and texture.palette[255] == (0, 0, 255)
    back = read_wad(write_wad([texture]))
    assert len(back) == 1 and back[0].lump_type == SPRAY
    rgba = back[0].rgba()
    assert np.array_equal(rgba, texture.rgba())
    assert rgba[0, 0, 3] == 0 and tuple(rgba[56, 56]) == (20, 220, 40, 255)
    with pytest.raises(ValueError):
        make_spray(_logo(), (100, 100))


def test_mip_levels_and_plain_textures() -> None:
    indices = np.arange(32 * 16, dtype=np.uint8).reshape(16, 32) % 7
    palette = [(i * 30, 0, 0) for i in range(7)] + [(0, 0, 0)] * 249
    texture = Texture("brick", 32, 16, indices.tobytes(), palette, MIPTEX)
    data = write_wad([texture, Texture("{fence", 16, 16, bytes([255]) * 256, palette)])
    textures = read_wad(data)
    assert [t.name for t in textures] == ["brick", "{fence"]
    assert textures[0].rgba()[0, 1, 0] == 30 and textures[1].rgba()[..., 3].max() == 0
    # the lump: 40-byte header + 4 levels + palette
    assert len(data) == 12 + 2 * 32 + (40 + 512 + 128 + 32 + 8 + 2 + 768 + 2) + \
        (40 + 256 + 64 + 16 + 4 + 2 + 768 + 2)
    with pytest.raises(WadError):
        read_wad(b"WAD2" + bytes(8))


def test_alpha_decal() -> None:
    ramp = [(255 - i, 255 - i, 255 - i) for i in range(255)] + [(200, 0, 0)]
    decal = Texture("{blood", 16, 16, bytes(range(256)), ramp)
    assert decal.alpha_decal
    rgba = decal.rgba()
    assert tuple(rgba[0, 0]) == (200, 0, 0, 0) and tuple(rgba[-1, -1]) == (200, 0, 0, 255)


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_spray_dialog_and_viewer(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from PySide6.QtGui import QImage

    from valve_qc_merger.preview.render import decode_png
    from valve_qc_merger.studio.wad_tools import SprayDialog, WadViewer
    logo = _logo(200)
    image = QImage(logo.tobytes(), 200, 200, 800, QImage.Format.Format_RGBA8888)
    image.save(str(tmp_path / "logo.png"))
    dialog = SprayDialog(str(tmp_path))
    dialog.path_edit.setText(str(tmp_path / "logo.png"))
    assert dialog.size_box.count() == 2  # largest, any server (200 px: no scaling up)
    assert dialog.size_box.currentData() == (112, 112)
    dialog.size_box.setCurrentIndex(dialog.size_box.count() - 1)
    assert dialog.texture is not None and dialog.texture.width == 64
    saved = dialog.save(tmp_path / "tempdecal.wad")
    assert saved is not None and read_wad(saved)[0].width == 64
    viewer = WadViewer(str(saved))
    assert viewer.grid.count() == 1
    viewer.grid.setCurrentRow(0)
    assert "{LOGO: 64 × 64" in viewer.info.text()
    png = viewer.export_current(tmp_path / "logo_out.png")
    assert png is not None and decode_png(png.read_bytes()).shape == (64, 64, 4)
